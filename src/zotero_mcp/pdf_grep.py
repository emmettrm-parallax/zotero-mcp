"""
Page-anchored full-text search of one PDF.

``grep_pdf`` answers "on which pages does this term occur, and in what words?"
without reading the whole document: page text is extracted once, normalised,
cached on disk, and searched with a regular expression.

Normalisation (``NORMALIZER_VERSION``) is what makes a plain term find what a
person sees on the page:

- MuPDF expands ligature glyphs itself (``TEXT_PRESERVE_LIGATURES`` is off); the
  U+FB00-U+FB06 code points are expanded again for fonts that map them by hand.
- A word hyphenated at a line end ("pres-" / "sure") is joined. ``TEXT_DEHYPHENATE``
  is passed to MuPDF, but MuPDF 1.28 leaves the hyphen in plain-text output, so
  ``normalize_text`` joins the line break itself: letter, hyphen, newline,
  lowercase letter. An uppercase next letter ("Navier-" / "Stokes") stays a compound.
- Unicode dashes (U+2010-U+2015, U+2212) become ``-``.
- Every whitespace run becomes one space.
- No NFKC: it turns 10⁻³ into 10-3 and would corrupt exponents and units.

Matching runs on the normalised text, so offsets stay valid for the snippets.
A literal term is escaped and its tokens are joined with ``[\\s\\-]*``: the term
"carry-over" also finds "carryover" and "carry over".

Pages are cached as gzip JSON under ``${XDG_CACHE_HOME:-~/.cache}/zotero-mcp/pagetext/``.
The key is path, size, mtime_ns and normaliser version, so an edited file or a
new normaliser never reads stale text. Above ``POOL_MIN_PAGES`` pages the
extraction runs in a process pool and falls back to a serial pass on any pool
error.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pymupdf

# Bump when the extraction flags or the normalisation change: it invalidates every cache entry.
NORMALIZER_VERSION = 1

POOL_MIN_PAGES = 150  # serial extraction is faster below this; spawning workers has a fixed cost
MARK_OPEN = "[["
MARK_CLOSE = "]]"

_TEXT_FLAGS = (pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES) | pymupdf.TEXT_DEHYPHENATE

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
_LINE_HYPHEN = re.compile(r"(?<=[^\W\d_])-[^\S\n]*\n\s*(?=[^\W\d_A-Z])")
# A literal term splits on whitespace and hyphens; the pieces are re-joined with _JOINER.
_TOKEN_SPLIT = re.compile(r"[\s\-]+")
_JOINER = r"[\s\-]*"


class BadRegexError(ValueError):
    """A term that cannot be compiled (or is empty). ``code`` is the CLI error code."""

    code = "bad_regex"


def normalize_text(text: str) -> str:
    """Ligatures expanded, dashes unified, line-end hyphens joined, whitespace collapsed. Never NFKC."""
    text = _LINE_HYPHEN.sub("", text.translate(_TRANSLATE))
    return _WHITESPACE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Page text: extraction and cache
# ---------------------------------------------------------------------------

def _page_label(page) -> str:
    try:
        return page.get_label() or ""
    except Exception:
        return ""


def _extract_range(pdf_path: str, start: int, end: int) -> tuple[list[str], list[str]]:
    """Normalised text and labels for 0-based pages ``start`` to ``end - 1``.

    Module level so a worker process can import it.
    """
    try:  # a worker inherits stdout; MuPDF warnings must not land in --json output
        pymupdf.set_messages(fd=2)
    except Exception:
        pass
    texts: list[str] = []
    labels: list[str] = []
    doc = pymupdf.open(pdf_path)
    try:
        for index in range(start, end):
            page = doc[index]
            texts.append(normalize_text(page.get_text("text", flags=_TEXT_FLAGS)))
            labels.append(_page_label(page))
    finally:
        doc.close()
    return texts, labels


def _extract_parallel(pdf_path: str, page_count: int, jobs: int | None) -> tuple[list[str], list[str]]:
    workers = max(1, int(jobs or os.cpu_count() or 1))
    workers = min(workers, max(1, page_count // 25))
    size = -(-page_count // workers)
    bounds = [(lo, min(lo + size, page_count)) for lo in range(0, page_count, size)]
    texts: list[str] = []
    labels: list[str] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_extract_range, pdf_path, lo, hi) for lo, hi in bounds]
        for future in futures:  # in submit order, so pages stay in order
            chunk_texts, chunk_labels = future.result()
            texts.extend(chunk_texts)
            labels.extend(chunk_labels)
    if len(texts) != page_count:
        raise RuntimeError(f"pool returned {len(texts)} pages, expected {page_count}")
    return texts, labels


def _extract_pages(pdf_path: str, page_count: int, jobs: int | None) -> tuple[list[str], list[str]]:
    """All pages' text and labels: a pool above ``POOL_MIN_PAGES``, serial on any pool failure."""
    if page_count > POOL_MIN_PAGES:
        try:
            return _extract_parallel(pdf_path, page_count, jobs)
        except Exception:
            pass  # sandbox without semaphores, a broken pool, a worker crash: serial still works
    return _extract_range(pdf_path, 0, page_count)


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "zotero-mcp" / "pagetext"


def _cache_file(pdf_path: str) -> Path | None:
    """The cache file for this exact file state, or None when the file cannot be stat'ed."""
    try:
        stat = os.stat(pdf_path)
    except OSError:
        return None
    path_key = hashlib.sha256(os.path.abspath(pdf_path).encode()).hexdigest()[:24]
    state_key = hashlib.sha256(
        f"{stat.st_size}|{stat.st_mtime_ns}|{NORMALIZER_VERSION}".encode()
    ).hexdigest()[:16]
    return _cache_dir() / f"{path_key}-{state_key}.json.gz"


def _cache_read(cache_file: Path | None, page_count: int) -> tuple[list[str], list[str]] | None:
    if cache_file is None or not cache_file.is_file():
        return None
    try:
        with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        texts, labels = payload["pages"], payload["labels"]
        if payload["normalizer"] != NORMALIZER_VERSION or len(texts) != page_count or len(labels) != page_count:
            return None
        return texts, labels
    except Exception:
        return None  # a truncated or foreign file is a miss, not an error


def _cache_write(cache_file: Path | None, texts: list[str], labels: list[str]) -> None:
    """Best effort: a read-only cache directory must not fail the search."""
    if cache_file is None:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        prefix = cache_file.name.split("-", 1)[0] + "-"
        for stale in cache_file.parent.glob(prefix + "*"):  # older states of this same path
            if stale != cache_file:
                stale.unlink(missing_ok=True)
        tmp = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            json.dump({"normalizer": NORMALIZER_VERSION, "pages": texts, "labels": labels}, handle,
                      ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, cache_file)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

def _compile_term(term: str, *, regex: bool, word: bool) -> re.Pattern:
    if not term or not term.strip():
        raise BadRegexError("Empty search term")
    if regex:
        body = term
    else:
        tokens = [re.escape(tok) for tok in _TOKEN_SPLIT.split(normalize_text(term)) if tok]
        if not tokens:  # a term made only of dashes and spaces
            body = re.escape(normalize_text(term))
        else:
            body = _JOINER.join(tokens)
    if word:
        body = rf"(?<!\w)(?:{body})(?!\w)"
    try:
        return re.compile(body, re.IGNORECASE)
    except re.error as exc:
        raise BadRegexError(f"Invalid regular expression {term!r}: {exc}") from exc


def _page_ranges(pages, page_count: int) -> list[int] | None:
    """The 0-based page indexes a filter selects, or None for every page."""
    if pages is None:
        return None
    selected: set[int] = set()
    for item in pages:
        start, end = (item, item) if isinstance(item, int) else item
        if start > end:
            start, end = end, start
        if start < 1 or start > page_count:
            raise ValueError(f"Page range {start}-{end} out of range (PDF has {page_count} pages)")
        selected.update(range(start - 1, min(end, page_count)))
    return sorted(selected)


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------

def _union(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _build_snippets(text: str, hits: list[tuple[int, int, int]], terms: list[str], context: int) -> list[dict]:
    """One snippet per group of overlapping windows, matches marked ``[[ ]]``.

    ``hits`` is ``(start, end, term_index)`` for every match on the page.
    """
    hits = sorted(hits)
    groups: list[list[int]] = []  # [window_start, window_end, first_hit, last_hit)
    for number, (start, end, _term) in enumerate(hits):
        lo, hi = max(0, start - context), min(len(text), end + context)
        if groups and lo <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], hi)
            groups[-1][3] = number + 1
        else:
            groups.append([lo, hi, number, number + 1])
    snippets = []
    for lo, hi, first, last in groups:
        members = hits[first:last]
        pieces: list[str] = []
        cursor = lo
        for start, end in _union([(s, e) for s, e, _ in members]):
            pieces.append(text[cursor:start])
            pieces.append(MARK_OPEN + text[start:end] + MARK_CLOSE)
            cursor = end
        pieces.append(text[cursor:hi])
        found = sorted({term for _s, _e, term in members})
        snippets.append({"text": "".join(pieces).strip(), "terms": [terms[i] for i in found]})
    return snippets


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def grep_pdf(
    pdf_path,
    terms,
    *,
    regex=False,
    word=False,
    pages=None,
    context=300,
    max_hits=200,
    order="page",
    use_cache=True,
    jobs=None,
) -> dict:
    """Search one PDF for ``terms`` and return per-page hits with snippets.

    Args:
        pdf_path: Path to the PDF.
        terms: One term or a list of terms. Literal by default (case-insensitive,
            hyphen- and space-tolerant); ``regex`` uses each term as given.
        word: Require a word boundary on both ends of each match.
        pages: List of ``(start, end)`` 1-based inclusive ranges, or None for all pages.
        context: Characters of page text on each side of a match.
        max_hits: Upper bound on the snippets emitted. ``counts`` is never affected.
        order: ``"page"`` (ascending) or ``"score"`` (most hits first, ties by page).
        use_cache: False bypasses the page-text cache (no read, no write).
        jobs: Worker processes for a large PDF (default: ``os.cpu_count()``).

    Raises:
        BadRegexError: A term is empty or is not a valid regular expression.
        ValueError: ``order`` is unknown or a page range lies outside the PDF.

    ``key``, ``attachment_key`` and ``title`` are left None for the caller to fill.
    """
    started = time.perf_counter()
    if order not in ("page", "score"):
        raise ValueError(f"order must be 'page' or 'score', got {order!r}")
    term_list = [terms] if isinstance(terms, str) else list(terms)
    term_list = list(dict.fromkeys(term_list))  # duplicates would double nothing but clutter counts
    if not term_list:
        raise BadRegexError("No search terms")
    patterns = [_compile_term(term, regex=regex, word=word) for term in term_list]

    pdf_path = str(pdf_path)
    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass:
            raise ValueError("PDF is password protected")
        page_count = len(doc)
    finally:
        doc.close()
    selected = _page_ranges(pages, page_count)

    cache_file = _cache_file(pdf_path) if use_cache else None
    cached = _cache_read(cache_file, page_count) if use_cache else None
    if cached is not None:
        cache_state = "hit"
        texts, labels = cached
    elif use_cache:
        cache_state = "miss"
        texts, labels = _extract_pages(pdf_path, page_count, jobs)
        _cache_write(cache_file, texts, labels)
    else:
        cache_state = "off"
        if selected is None:
            texts, labels = _extract_pages(pdf_path, page_count, jobs)
        else:  # nothing is stored, so only the requested pages are worth extracting
            texts, labels = [""] * page_count, [""] * page_count
            for index in selected:
                page_texts, page_labels = _extract_range(pdf_path, index, index + 1)
                texts[index], labels[index] = page_texts[0], page_labels[0]

    counts = {term: 0 for term in term_list}
    page_rows: list[dict] = []
    for index in (range(page_count) if selected is None else selected):
        text = texts[index]
        if not text:
            continue
        hits: list[tuple[int, int, int]] = []
        per_term: dict[str, int] = {}
        for number, pattern in enumerate(patterns):
            found = [(m.start(), m.end(), number) for m in pattern.finditer(text) if m.end() > m.start()]
            if found:
                hits.extend(found)
                per_term[term_list[number]] = len(found)
                counts[term_list[number]] += len(found)
        if hits:
            page_rows.append({
                "page": index + 1,
                "label": labels[index],
                "hits": len(hits),
                "terms": per_term,
                "_text": text,
                "_hits": hits,
            })

    if order == "score":
        page_rows.sort(key=lambda row: (-row["hits"], row["page"]))

    budget = max(0, int(max_hits))
    truncated = False
    for row in page_rows:
        snippets = _build_snippets(row.pop("_text"), row.pop("_hits"), term_list, max(0, int(context)))
        if len(snippets) > budget:
            truncated = True
        row["snippets"] = snippets[:budget]
        budget -= len(row["snippets"])

    return {
        "key": None,
        "attachment_key": None,
        "title": None,
        "page_count": page_count,
        "terms": term_list,
        "counts": counts,
        "total_hits": sum(counts.values()),
        "pages_with_hits": len(page_rows),
        "truncated": truncated,
        "cache": cache_state,
        "seconds": round(time.perf_counter() - started, 3),
        "pages": page_rows,
    }


def format_grep_markdown(data: dict) -> str:
    """The ``grep_pdf`` result as Markdown: a summary, then each page with its snippets."""
    title = data.get("title") or data.get("key") or "PDF"
    lines = [f"# grep: {title}", ""]
    counts = data.get("counts") or {}
    summary = ", ".join(f"{term} {count}" for term, count in counts.items())
    lines.append(
        f"{data.get('total_hits', 0)} hits on {data.get('pages_with_hits', 0)} of "
        f"{data.get('page_count', 0)} pages ({summary})"
    )
    lines.append(f"cache {data.get('cache', 'off')}, {data.get('seconds', 0)} s")
    if data.get("truncated"):
        lines.append("Snippets were cut by max-hits. The counts above are complete.")
    for row in data.get("pages") or []:
        label = row.get("label") or ""
        page_name = f"p. {row['page']}" + (f" [{label}]" if label and label != str(row["page"]) else "")
        per_term = ", ".join(f"{term} {count}" for term, count in (row.get("terms") or {}).items())
        lines.extend(["", f"## {page_name}: {row['hits']} hits ({per_term})"])
        for snippet in row.get("snippets") or []:
            lines.append(f"- {snippet['text']}")
    return "\n".join(lines) + "\n"
