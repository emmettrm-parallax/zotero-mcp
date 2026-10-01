"""Term search over a source index, for ``zotero-cli index show --grep`` and ``index search``.

A research agent asks "what does this source say about X?" It should not pull the whole index
for the answer. ``apply_filters`` runs the whole chain on an index that ``show_index`` returns:
pages, vocabulary cut, term match, limit, projection. Every function here is pure. It takes
dicts and returns new dicts. The imports are the standard library, ``text_match``,
``index_slice`` and ``cli_json``, so the index path never loads pymupdf.

Match rule (the same as ``pdf_grep``): ``text_match.normalize_text`` normalises each field
value. A literal term of 3 or more characters ignores case and joins its words with
``[\\s\\-]*``, so "carry-over" also finds "carryover" and "carry over". A literal term of 1 or
2 characters matches a whole word and keeps case, so "Cd" does not hit every "cd". With
``regex`` a term is a pattern with ``re.IGNORECASE``. A term never spans two fields, because
each field gets its own search. The fields also join into one string with ``"\\x1f"`` for a
cheap first test. ``\\s`` matches ``"\\x1f"`` in Python, so a hit on the joined string counts
only when it stays inside one field.

Fields that match, by kind:

* facts: statement, quantity, symbol, value, unit, condition, validity, ref, quote
* vocabulary: term, symbol, variants[], meaning
* tables_figures: label, caption
* equations: label, latex, validity, variables[].symbol, variables[].meaning
* gaps: kind, note

Ids, page numbers, ``section_id``, ``found_by``, ``confidence`` and ``value_num`` never match.
Terms never cut ``sections``.

Expansion (``expand``): the vocabulary works as a synonym table. Each user term borrows the
forms of the entries whose term, variants or symbol match it. A form is the term, a variant, or
one part of the symbol (the symbol split on ","). A term that matches more than
``MAX_EXPAND_ENTRIES`` entries is too broad. It goes to ``broad_terms`` and does not expand. A
form whose pattern key equals a user term or an earlier form is dropped. The pattern key is the
edge hyphens plus the casefolded words, so "brush-seal" adds nothing to "brush seal". A form of
3 or more characters with a letter uses the literal rule. A shorter form is a symbol. It
matches only a symbol field, by exact equality with case, because "s" or "n" would hit every
fact.

A ``lead`` record leaves out each key whose value is null. A ``full`` record stays as it is. A
fact with no quantity and no value gains a ``lead`` key: the first 12 words of its statement, or
of its text when it has no statement, with `` ...`` added when the words run past 12.
"""

from __future__ import annotations

import copy
import re

from zotero_mcp.cli_json import CliError
from zotero_mcp.index_slice import filter_index_pages
from zotero_mcp.text_match import BadRegexError, compile_term, normalize_text

KINDS = ("facts", "vocabulary", "tables_figures", "equations", "gaps")
MAX_EXPAND_ENTRIES = 25  # a user term that matches more vocabulary entries is not expanded
REPORT_EXPANDED_CAP = 80  # expanded_terms in the report. expanded_total keeps the full count
HIT_CAP = 3  # "hit" lists at most this many terms. The rank reads len(hit), so it stops at this number
SHOW_LIMIT = 40  # entries per kind, index show
SEARCH_LIMIT = 10  # entries per kind, index search

#: The keys a ``lead`` record keeps. A key that the entry lacks, or holds as null, is left out.
LEAD_FIELDS = {
    "facts": ("id", "page", "kind", "quantity", "value", "unit", "condition", "ref", "section_id"),
    "vocabulary": ("term", "kind", "symbol", "variants", "pages"),
    "tables_figures": ("id", "page", "label", "caption"),
    "equations": ("id", "page", "label", "latex"),
    "gaps": ("id", "page", "kind"),
}

_SHORT_TERM = 3  # a literal term shorter than this is a whole-word, case-sensitive match
_SEP = "\x1f"

# The fields that match. A list value (variants) counts one field for each item.
_TEXT_FIELDS = {
    "facts": ("statement", "quantity", "symbol", "value", "unit", "condition", "validity", "ref", "quote"),
    "vocabulary": ("term", "symbol", "variants", "meaning"),
    "tables_figures": ("label", "caption"),
    "equations": ("label", "latex", "validity"),
    "gaps": ("kind", "note"),
}
_VARIABLE_FIELDS = ("symbol", "meaning")

_TOKEN_SPLIT = re.compile(r"[\s\-]+")
# re.IGNORECASE equates these non-ASCII letters with an ASCII one (U+212A, the Kelvin sign, is not
# listed: str.lower() folds it). str.lower() alone would turn U+0130 into two characters.
_FOLD_EXTRA = {0x130: "i", 0x131: "i", 0x17F: "s"}


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

def parse_terms(values: list[str], *, regex: bool) -> list[str]:
    """The search terms from the repeated ``--grep`` (or positional) values.

    Without ``regex``, each value splits on ",". The function strips each part and drops the empty
    parts. It also drops a duplicate (the same casefold of ``normalize_text``) and keeps the first.
    With ``regex``, each value is one pattern and stays as typed. The function drops a blank
    pattern and an exact duplicate. It raises ``CliError`` with the code ``bad_grep`` when no term
    is left.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        for part in ([value] if regex else value.split(",")):
            if not part.strip():
                continue
            part = part if regex else part.strip()
            key = part if regex else normalize_text(part).casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(part)
    if not terms:
        raise CliError("no search terms: give at least one non-empty term", code="bad_grep")
    return terms


def _compile(term: str, **kwargs) -> re.Pattern:
    try:
        return compile_term(term, **kwargs)
    except BadRegexError as exc:
        raise CliError(str(exc), code="bad_regex") from exc


def check_terms(terms, *, regex: bool) -> None:
    """Compile every term the way the search will. Raises ``CliError`` with the code ``bad_regex``."""
    for term in terms:
        _Matcher(term, regex=regex)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

def _as_text(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _field_values(kind: str, entry: dict) -> list[str]:
    """The normalised, non-empty field values that a term can match, one string for each field."""
    raw: list = []
    for name in _TEXT_FIELDS[kind]:
        value = entry.get(name)
        raw.extend(value if isinstance(value, list) else [value])
    if kind == "equations" and isinstance(entry.get("variables"), list):
        for variable in entry["variables"]:
            if isinstance(variable, dict):
                raw.extend(variable.get(name) for name in _VARIABLE_FIELDS)
    fields = []
    for value in raw:
        text = _as_text(value)
        text = normalize_text(text) if text is not None else ""
        if text:
            fields.append(text)
    return fields


def _symbol_parts(value) -> list[str]:
    text = _as_text(value)
    if text is None:
        return []
    return [part for part in (normalize_text(p) for p in text.split(",")) if part]


def _entry_symbols(kind: str, entry: dict) -> list[str]:
    """The symbol strings that a short expansion form must equal."""
    if kind in ("facts", "vocabulary"):
        return _symbol_parts(entry.get("symbol"))
    if kind == "equations" and isinstance(entry.get("variables"), list):
        symbols = []
        for variable in entry["variables"]:
            text = _as_text(variable.get("symbol")) if isinstance(variable, dict) else None
            text = normalize_text(text) if text is not None else ""
            if text:
                symbols.append(text)
        return symbols
    return []


def _expansion_fields(entry: dict) -> list[str]:
    """The forms of a vocabulary entry, in order: the term, the variants, the symbol parts."""
    forms = []
    for value in [entry.get("term")] + (entry["variants"] if isinstance(entry.get("variants"), list) else []):
        text = _as_text(value)
        text = normalize_text(text) if text is not None else ""
        if text:
            forms.append(text)
    forms.extend(_symbol_parts(entry.get("symbol")))
    return forms


def _pattern_key(text: str) -> tuple:
    """The identity of a literal pattern: two texts with one key match the same strings (case aside).

    The key is the edge hyphens plus the casefolded words. "brush seal", "brush-seal" and
    "Brush  Seal" share a key. "brushseal" does not, because its pattern has no word join.
    """
    plain = normalize_text(text)
    words = tuple(word.casefold() for word in _TOKEN_SPLIT.split(plain) if word)
    if not words:  # only dashes and spaces
        return (False, (plain,), False)
    return (plain.startswith("-"), words, plain.endswith("-"))


class _Hay:
    """The fields of one entry, and the same fields joined for a first, cheap test."""

    __slots__ = ("fields", "text", "_folded")

    def __init__(self, fields: list[str]):
        self.fields = fields
        self.text = _SEP.join(fields)
        self._folded: str | None = None

    @property
    def folded(self) -> str:
        """Lower case, with the non-ASCII letters that ``re.IGNORECASE`` equates with i, s and k folded."""
        if self._folded is None:
            self._folded = self.text.translate(_FOLD_EXTRA).lower()
        return self._folded


class _Matcher:
    """One term, compiled by the match rule.

    ``needle`` is a plain substring that every match of the pattern must contain. Testing it
    first skips the regex for the many entries that cannot match.
    """

    __slots__ = ("term", "pattern", "regex", "needle", "fold")

    def __init__(self, term: str, *, regex: bool):
        self.term = term
        self.regex = regex
        self.needle: str | None = None
        self.fold = False
        if regex:
            self.pattern = _compile(term, regex=True)
            return
        plain = normalize_text(term)
        tokens = [tok for tok in _TOKEN_SPLIT.split(plain) if tok]
        if len(plain) < _SHORT_TERM:
            self.pattern = _compile(term, word=True, ignore_case=False)
            self.needle = max(tokens, key=len) if tokens else None
        else:
            self.pattern = _compile(term)
            ascii_tokens = [tok for tok in tokens if tok.isascii()]  # only ASCII folds exactly
            self.needle = max(ascii_tokens, key=len).lower() if ascii_tokens else None
            self.fold = True

    def match(self, hay: _Hay) -> bool:
        if self.regex:  # an anchor or a dot must not see the neighbour field: search each on its own
            return any(m.end() > m.start() for field in hay.fields for m in self.pattern.finditer(field))
        if self.needle is not None and self.needle not in (hay.folded if self.fold else hay.text):
            return False
        found = self.pattern.search(hay.text)
        if found is None:
            return False
        if _SEP not in found.group():
            return True
        # "\s" matches the separator: a hit that spans two fields is void, and it may hide a real one.
        return any(self.pattern.search(field) for field in hay.fields)


# ---------------------------------------------------------------------------
# Match and expansion
# ---------------------------------------------------------------------------

def _expansion(terms: list[str], users: list[_Matcher], vocabulary: list) -> tuple[list[_Matcher], list[str], list[str]]:
    """The expansion forms ``(literal matchers, symbols, broad_terms)`` for the user terms."""
    entries = [(entry, _Hay(fields)) for entry in vocabulary if isinstance(entry, dict)
               for fields in [_expansion_fields(entry)] if fields]
    user_keys = {_pattern_key(term) for term in terms}
    forms: list[_Matcher] = []
    form_keys: set[tuple] = set()
    symbols: list[str] = []
    broad: list[str] = []
    for term, matcher in zip(terms, users):
        lending = [entry for entry, hay in entries if matcher.match(hay)]
        if len(lending) > MAX_EXPAND_ENTRIES:
            broad.append(term)
            continue
        for entry in lending:
            for form in _expansion_fields(entry):
                key = _pattern_key(form)
                if key in user_keys:
                    continue
                if len(form) < _SHORT_TERM:
                    if form not in symbols:
                        symbols.append(form)
                elif any(ch.isalpha() for ch in form) and key not in form_keys:
                    form_keys.add(key)
                    forms.append(_Matcher(form, regex=False))
    return forms, symbols, broad


def filter_index_terms(index: dict, terms, *, regex: bool = False, expand: bool = False, vocabulary=None):
    """The entries of ``index`` that match ``terms``, and a report.

    Returns ``(subset, report)``. ``subset`` is ``index`` with each of the five arrays cut to the
    entries that match. Each kept entry is a shallow copy with a ``hit`` list: the matched user
    terms in term order, then the matched expanded forms, then the symbols, at most ``HIT_CAP``.
    Other keys pass unchanged. ``vocabulary`` is the table that ``expand`` reads (default: the
    vocabulary of ``index``). The caller passes the vocabulary from before a page cut.

    The report has ``terms``, ``expanded_terms`` (the first ``REPORT_EXPANDED_CAP``),
    ``expanded_total``, ``expanded_symbols``, ``broad_terms``, ``term_counts`` (the entries of all
    kinds for each user term), ``total`` (the entries before the match) and ``matched``. The last
    two go by kind.

    Raises ``CliError`` with the code ``bad_regex`` for a term that does not compile. The input
    does not change.
    """
    terms = list(terms or [])
    users = [_Matcher(term, regex=regex) for term in terms]
    forms: list[_Matcher] = []
    symbols: list[str] = []
    broad: list[str] = []
    if expand and users:
        source = vocabulary if vocabulary is not None else index.get("vocabulary")
        forms, symbols, broad = _expansion(terms, users, source if isinstance(source, list) else [])
    symbol_rank = {symbol: number for number, symbol in enumerate(symbols)}

    counts = [0] * len(users)
    total = {kind: 0 for kind in KINDS}
    matched = {kind: 0 for kind in KINDS}
    subset = dict(index)
    for kind in KINDS:
        entries = index.get(kind)
        if not isinstance(entries, list):
            continue
        total[kind] = len(entries)
        if not users:
            matched[kind] = len(entries)
            subset[kind] = list(entries)
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            hay = _Hay(_field_values(kind, entry))
            hits = []
            for number, matcher in enumerate(users):
                if matcher.match(hay):
                    counts[number] += 1
                    hits.append(matcher.term)
            for form in forms:
                if len(hits) >= HIT_CAP:
                    break
                if form.match(hay):
                    hits.append(form.term)
            if symbols and len(hits) < HIT_CAP:
                own = {s for s in _entry_symbols(kind, entry) if s in symbol_rank}
                hits.extend(sorted(own, key=symbol_rank.__getitem__))
            if hits:
                kept.append({**entry, "hit": hits[:HIT_CAP]})
        matched[kind] = len(kept)
        subset[kind] = kept

    report = {
        "terms": terms,
        "expanded_terms": [form.term for form in forms[:REPORT_EXPANDED_CAP]],
        "expanded_total": len(forms),
        "expanded_symbols": symbols,
        "broad_terms": broad,
        "term_counts": dict(zip(terms, counts)),
        "total": total,
        "matched": matched,
    }
    return subset, report


# ---------------------------------------------------------------------------
# Limit, pages, projection
# ---------------------------------------------------------------------------

def _rank(entry, user_terms: set[str], position: int) -> tuple[int, int, int]:
    hit = entry.get("hit") if isinstance(entry, dict) else None
    hit = hit if isinstance(hit, list) else []
    return (0 if hit and hit[0] in user_terms else 1, -len(hit), position)


def limit_index(index: dict, limit: int, *, terms=()):
    """Cut each of the five arrays to ``limit`` entries. ``0`` means no cap.

    Returns ``(subset, returned, truncated)``. The last two go by kind. With ``terms`` (the user
    terms that made the ``hit`` lists), the rank of an entry is ``(0 if a user term hit else 1,
    -len(hit), index position)``. Without them, the rank is the index position. The first key
    decides first, so an entry with a user-term hit always beats an entry with expanded hits only.
    A ``hit`` list stops at ``HIT_CAP``, and so does ``len(hit)``. The kept entries stay in index
    order. ``truncated`` is the number of entries cut. The input does not change.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError(f"limit must be an integer of 0 or more, got {limit!r}")
    user_terms = set(terms)
    subset = dict(index)
    returned = {kind: 0 for kind in KINDS}
    truncated = {kind: 0 for kind in KINDS}
    for kind in KINDS:
        entries = index.get(kind)
        if not isinstance(entries, list):
            continue
        if limit and len(entries) > limit:
            if user_terms:
                order = sorted(range(len(entries)), key=lambda i: _rank(entries[i], user_terms, i))
                keep = sorted(order[:limit])
            else:
                keep = range(limit)
            kept = [entries[i] for i in keep]
        else:
            kept = list(entries)
        subset[kind] = kept
        returned[kind] = len(kept)
        truncated[kind] = len(entries) - len(kept)
    return subset, returned, truncated


def vocabulary_on_pages(index: dict, pages: list[int]) -> dict:
    """A copy of ``index`` whose vocabulary keeps the entries that appear on any of ``pages``.

    An entry with no pages is dropped. The other keys pass unchanged. This function does not check
    the pages. ``index_slice.filter_index_pages`` does that.
    """
    wanted = {page for page in pages if isinstance(page, int) and not isinstance(page, bool)}
    out = dict(index)
    if isinstance(index.get("vocabulary"), list):
        out["vocabulary"] = [
            entry for entry in index["vocabulary"]
            if isinstance(entry, dict) and isinstance(entry.get("pages"), list) and wanted.intersection(
                page for page in entry["pages"] if isinstance(page, int) and not isinstance(page, bool)
            )
        ]
    return out


def _blank(value) -> bool:
    """True for ``None`` or an all-whitespace string. A number, including ``0``, is not blank."""
    return value is None or (isinstance(value, str) and not value.strip())


def _lead(kind: str, entry):
    if not isinstance(entry, dict):
        return copy.deepcopy(entry)
    lead = {name: copy.deepcopy(entry[name]) for name in LEAD_FIELDS[kind] if entry.get(name) is not None}
    if "hit" in entry:
        lead["hit"] = list(entry["hit"])
    if kind == "facts" and _blank(entry.get("quantity")) and _blank(entry.get("value")):
        source = entry.get("statement") or entry.get("text")
        if source:
            words = source.split()
            cut = " ..." if len(words) > 12 else ""
            lead["lead"] = " ".join(words[:12]) + cut
    return lead


def project_index(index: dict, fields: str = "lead") -> dict:
    """The index with each record cut to its ``fields``: ``"lead"`` or ``"full"``.

    ``full`` is a deep copy: the records stay as they are, with their ``hit``. ``lead`` keeps the
    ``LEAD_FIELDS`` of each record and its ``hit``, and leaves out each key whose value is null.
    A fact record with no quantity and no value also gains a ``lead`` key: the first 12 words of
    its statement, or of its text when the statement is blank, with `` ...`` added when the words
    run past 12. ``header``, ``sections``, ``schema`` and any unknown key pass in full. The
    result shares nothing with the input.
    """
    if fields == "full":
        return copy.deepcopy(index)
    if fields != "lead":
        raise ValueError(f"fields must be 'lead' or 'full', got {fields!r}")
    out = {}
    for key, value in index.items():
        if key in LEAD_FIELDS and isinstance(value, list):
            out[key] = [_lead(key, entry) for entry in value]
        else:
            out[key] = copy.deepcopy(value)
    return out


def apply_filters(index: dict, *, pages=None, terms=None, regex: bool = False, expand: bool = False,
                  fields: str = "full", limit: int = SHOW_LIMIT):
    """The whole chain on an index that ``show_index`` returned: ``(index, report)``.

    The steps are pages (``index_slice.filter_index_pages``, then the vocabulary cut to the entries
    on those pages), term match, limit, projection. All filters AND. ``expand`` reads the
    vocabulary from before the page cut. The report is the ``filter`` object of ``index show``
    without ``section``. Its keys are ``terms``, ``regex``, ``expand``, ``fields``, ``limit``,
    ``pages``, ``expanded_terms``, ``expanded_total``, ``expanded_symbols``, ``broad_terms``,
    ``term_counts``, ``total``, ``matched``, ``returned`` and ``truncated``. The last four go by
    kind, for all five kinds. ``total`` counts the entries after the pages cut and before the
    match. The input never changes.

    Raises ``CliError`` with the code ``bad_pages`` or ``bad_regex``. Raises ``ValueError`` for a
    bad ``fields`` or ``limit``.
    """
    if fields not in ("lead", "full"):
        raise ValueError(f"fields must be 'lead' or 'full', got {fields!r}")
    terms = list(terms or [])
    source = index.get("vocabulary")
    working = index
    if pages is not None:
        working = vocabulary_on_pages(filter_index_pages(index, pages), pages)
    subset, found = filter_index_terms(
        working, terms, regex=regex, expand=expand, vocabulary=source if isinstance(source, list) else None
    )
    limited, returned, truncated = limit_index(subset, limit, terms=terms)
    report = {
        "terms": found["terms"],
        "regex": regex,
        "expand": expand,
        "fields": fields,
        "limit": limit,
        "pages": list(pages) if pages is not None else None,
        "expanded_terms": found["expanded_terms"],
        "expanded_total": found["expanded_total"],
        "expanded_symbols": found["expanded_symbols"],
        "broad_terms": found["broad_terms"],
        "term_counts": found["term_counts"],
        "total": found["total"],
        "matched": found["matched"],
        "returned": returned,
        "truncated": truncated,
    }
    return project_index(limited, fields), report
