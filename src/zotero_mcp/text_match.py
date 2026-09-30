"""
Text normalisation and term matching for the PDF search and the source-index search.

This module uses only the standard library. ``pdf_grep`` imports pymupdf. ``index_grep``
must not. The matching rules live here, and both modules import them.

``normalize_text`` makes a plain term find what a person sees:

- It expands the U+FB00-U+FB06 ligature code points ("ﬁ" becomes "fi").
- It joins a word that a line end splits ("pres-" / "sure"). An uppercase next letter
  ("Navier-" / "Stokes") marks a compound. The hyphen stays and the line break goes.
- It changes the Unicode dashes (U+2010-U+2015, U+2212) to ``-``.
- It changes every whitespace run to one space.
- It does not use NFKC. NFKC turns 10⁻³ into 10-3 and would damage exponents and units.

``compile_term`` turns a term into a pattern. It escapes a literal term and joins its words
with ``[\\s\\-]*``. The term "carry-over" then also finds "carryover" and "carry over".

``pdf_grep.NORMALIZER_VERSION`` keys the page-text cache. Raise it when a change to this
module changes what ``normalize_text`` returns.
"""

from __future__ import annotations

import re

_LIGATURES = {
    0xFB00: "ff",
    0xFB01: "fi",
    0xFB02: "fl",
    0xFB03: "ffi",
    0xFB04: "ffl",
    0xFB05: "st",
    0xFB06: "st",
}
_DASHES = {code: "-" for code in range(0x2010, 0x2016)}
_DASHES[0x2212] = "-"
_TRANSLATE = {**_LIGATURES, **_DASHES}
_WHITESPACE = re.compile(r"\s+")
_LINE_HYPHEN = re.compile(r"(?<=[^\W\d_])-[^\S\n]*\n\s*(?=[^\W\d_])")
# A literal term splits on whitespace and hyphens; the pieces are re-joined with _JOINER.
_TOKEN_SPLIT = re.compile(r"[\s\-]+")
_JOINER = r"[\s\-]*"


class BadRegexError(ValueError):
    """A term that cannot be compiled (or is empty). ``code`` is the CLI error code."""

    code = "bad_regex"


def _join_line_hyphen(match: re.Match) -> str:
    """Drop the hyphen before a lowercase letter (a split word); keep it before any other letter."""
    return "" if match.string[match.end()].islower() else "-"


def normalize_text(text: str) -> str:
    """Ligatures expanded, dashes unified, line-end hyphens joined, whitespace collapsed. Never NFKC."""
    text = _LINE_HYPHEN.sub(_join_line_hyphen, text.translate(_TRANSLATE))
    return _WHITESPACE.sub(" ", text).strip()


def compile_term(term: str, *, regex: bool = False, word: bool = False, ignore_case: bool = True) -> re.Pattern:
    """The pattern for one term.

    A literal term is normalised, escaped and hyphen- and space-tolerant. ``regex`` uses the
    term as given. ``word`` requires a word boundary on both ends. ``ignore_case=False`` makes
    the match case-sensitive.

    Raises:
        BadRegexError: The term is empty or is not a valid regular expression.
    """
    if not term or not term.strip():
        raise BadRegexError("Empty search term")
    if regex:
        body = term
    else:
        plain = normalize_text(term)
        tokens = [re.escape(tok) for tok in _TOKEN_SPLIT.split(plain) if tok]
        if not tokens:  # a term made only of dashes and spaces
            body = re.escape(plain)
        else:  # an edge hyphen is literal: "-40" must not find every "40"
            body = ("-" if plain.startswith("-") else "") + _JOINER.join(tokens) + ("-" if plain.endswith("-") else "")
    if word:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    try:
        return re.compile(body, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise BadRegexError(f"Invalid regular expression {term!r}: {exc}") from exc
