"""Tests for the page-anchored PDF search (``zotero_mcp.pdf_grep``).

PDFs are built with pymupdf in a tmp dir. Text sits on one wide page so a long
line is not clipped by the media box, and the page cache is redirected to a tmp
dir through XDG_CACHE_HOME.
"""

from __future__ import annotations

import os
from concurrent.futures import Future
from pathlib import Path

import pymupdf
import pytest

from zotero_mcp import pdf_grep
from zotero_mcp.pdf_grep import BadRegexError, format_grep_markdown, grep_pdf, normalize_text

WIDE = 3000  # points: a 400-character line at 10 pt fits without clipping


def make_pdf(path, pages, *, font_buffer=None, width=WIDE):
    """Write a PDF. ``pages`` is a list of pages, each a list of lines (or one string)."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=width, height=792)
        kwargs = {"fontsize": 10}
        if font_buffer:
            page.insert_font(fontname="testfont", fontbuffer=font_buffer)
            kwargs["fontname"] = "testfont"
        for number, line in enumerate([lines] if isinstance(lines, str) else lines):
            page.insert_text((10, 60 + 14 * number), line, **kwargs)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(home))
    return home / "zotero-mcp" / "pagetext"


@pytest.fixture
def pdf(tmp_path):
    return lambda pages, name="doc.pdf", **kw: make_pdf(tmp_path / name, pages, **kw)


@pytest.fixture(scope="module")
def glyph_font():
    """pymupdf's bundled fallback font, so no installed font is needed.

    It draws U+FB01-FB04 (fi, fl, ffi, ffl) and U+2010, U+2013-U+2015 as real glyphs. It has no
    U+FB00, U+FB05, U+FB06 or U+2212: ``TestNormalize`` covers those code points directly.
    """
    font = pymupdf.Font("cjk")
    for code_point in (0xFB01, 0xFB02, 0xFB03, 0xFB04, 0x2010, 0x2013, 0x2014, 0x2015):
        assert font.has_glyph(code_point), hex(code_point)
    return font.buffer


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_expands_every_ligature_code_point(self):
        text = "\ufb00 \ufb01 \ufb02 \ufb03 \ufb04 \ufb05 \ufb06"
        assert normalize_text(text) == "ff fi fl ffi ffl st st"

    def test_unifies_dashes(self):
        assert normalize_text("a\u2010b\u2011c\u2012d\u2013e\u2014f\u2015g\u2212h") == "a-b-c-d-e-f-g-h"

    def test_collapses_whitespace_runs(self):
        assert normalize_text("a \t\n  b\u00a0 c\n") == "a b c"

    def test_does_not_apply_nfkc(self):
        """NFKC would turn the superscript minus and cube into the plain text 10-3."""
        assert normalize_text("10\u207b\u00b3 m\u00b2") == "10\u207b\u00b3 m\u00b2"

    def test_joins_a_line_end_hyphen_before_lowercase(self):
        assert normalize_text("pres-\nsure rise") == "pressure rise"
        assert normalize_text("pres- \n  sure") == "pressure"

    def test_keeps_a_compound_with_an_uppercase_second_part(self):
        """A joined "NavierStokes" would not be found by the term "Navier-Stokes"."""
        assert normalize_text("Navier-\nStokes") == "Navier-Stokes"
        assert normalize_text("the Navier- \n  Stokes equations") == "the Navier-Stokes equations"
        assert normalize_text("Fourier-\n\u00c9tienne") == "Fourier-\u00c9tienne"

    def test_dehyphenation_and_compound_side_by_side(self):
        assert normalize_text("pres-\nsure in a Navier-\nStokes solver") == "pressure in a Navier-Stokes solver"

    def test_keeps_the_hyphen_before_a_digit(self):
        assert normalize_text("Table 3-\n4") == "Table 3- 4"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

class TestMatching:
    def test_ligature_glyph_is_found_by_the_plain_word(self, pdf, glyph_font):
        path = pdf([["the \ufb02ow of air, an e\ufb03cient \ufb01lm and a bu\ufb04e"], ["nothing here"]],
                   font_buffer=glyph_font)
        raw = pymupdf.open(path)[0].get_text("text", flags=pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_PRESERVE_LIGATURES)
        assert all(ch in raw for ch in "\ufb01\ufb02\ufb03\ufb04"), "the fixture must really draw ligature glyphs"
        result = grep_pdf(path, ["flow", "efficient", "film", "buffle"], use_cache=False)
        assert result["counts"] == {"flow": 1, "efficient": 1, "film": 1, "buffle": 1}
        assert [row["page"] for row in result["pages"]] == [1]
        assert "[[flow]]" in result["pages"][0]["snippets"][0]["text"]

    def test_hyphenated_term_finds_hyphen_closed_and_open_spellings(self, pdf):
        path = pdf([["A carry-over loss."], ["A carryover loss."], ["A carry over loss."], ["No such thing."]])
        result = grep_pdf(path, "carry-over", use_cache=False)
        assert result["counts"] == {"carry-over": 3}
        assert [row["page"] for row in result["pages"]] == [1, 2, 3]

    @pytest.mark.parametrize("spelling", ["carry-over", "carryover", "carry over"])
    def test_each_spelling_alone_is_found_by_the_hyphenated_term(self, pdf, spelling):
        path = pdf([[f"A {spelling} loss."]])
        assert grep_pdf(path, "carry-over", use_cache=False)["total_hits"] == 1

    def test_closed_term_stays_closed(self, pdf):
        path = pdf([["A carry-over loss and a carryover loss."]])
        assert grep_pdf(path, "carryover", use_cache=False)["total_hits"] == 1

    def test_unicode_hyphen_in_the_page_matches_an_ascii_term(self, pdf, glyph_font):
        path = pdf([["carry\u2010over, carry\u2013over, carry\u2014over and carry\u2015over"]],
                   font_buffer=glyph_font)
        assert grep_pdf(path, "carry-over", use_cache=False)["counts"] == {"carry-over": 4}

    def test_hyphenated_line_break_matches_the_joined_word(self, pdf):
        path = pdf([["the gas pres-", "sure rise here"]])
        result = grep_pdf(path, "pressure", use_cache=False)
        assert result["counts"] == {"pressure": 1}
        assert "[[pressure]]" in result["pages"][0]["snippets"][0]["text"]

    def test_leading_hyphen_of_a_term_is_kept(self, pdf):
        """"-40" is a negative number: it must not find every plain "40"."""
        path = pdf([["from -40 to 40 and 140, carry-over"]])
        assert grep_pdf(path, "-40", use_cache=False)["total_hits"] == 1
        assert grep_pdf(path, "40", use_cache=False)["total_hits"] == 3
        assert grep_pdf(path, "-40 to 40", use_cache=False)["total_hits"] == 1
        assert grep_pdf(path, "carry-", use_cache=False)["total_hits"] == 1
        assert grep_pdf(path, "-", use_cache=False)["total_hits"] == 2

    def test_line_break_after_a_hyphen_keeps_a_capitalised_compound_findable(self, pdf):
        path = pdf([["the Navier-", "Stokes equations, and pres-", "sure rise"]])
        assert grep_pdf(path, "Navier-Stokes", use_cache=False)["total_hits"] == 1
        assert grep_pdf(path, "navier stokes", use_cache=False)["total_hits"] == 1
        result = grep_pdf(path, ["NavierStokes", "pressure"], use_cache=False)
        assert result["counts"] == {"NavierStokes": 0, "pressure": 1}
        text = grep_pdf(path, "Navier-Stokes", context=8, use_cache=False)["pages"][0]["snippets"][0]["text"]
        assert "[[Navier-Stokes]]" in text

    def test_match_is_case_insensitive(self, pdf):
        path = pdf([["Flow FLOW flow"]])
        assert grep_pdf(path, "FLoW", use_cache=False)["total_hits"] == 3

    def test_word_mode_rejects_substrings(self, pdf):
        path = pdf([["flow airflow flowing Flow."]])
        assert grep_pdf(path, "flow", use_cache=False)["counts"] == {"flow": 4}
        assert grep_pdf(path, "flow", word=True, use_cache=False)["counts"] == {"flow": 2}

    def test_word_mode_works_for_a_multiword_term(self, pdf):
        path = pdf([["pressure ratio and pressure ratios, high-pressure ratio"]])
        assert grep_pdf(path, "pressure ratio", use_cache=False)["counts"] == {"pressure ratio": 3}
        # "high-pressure ratio" still has a boundary before "pressure" (the hyphen is not a word char)
        assert grep_pdf(path, "pressure ratio", word=True, use_cache=False)["counts"] == {"pressure ratio": 2}

    def test_literal_term_is_escaped(self, pdf):
        path = pdf([["a.b axb a+b"]])
        assert grep_pdf(path, "a.b", use_cache=False)["counts"] == {"a.b": 1}
        assert grep_pdf(path, "a+b", use_cache=False)["counts"] == {"a+b": 1}

    def test_regex_mode_uses_the_term_as_given(self, pdf):
        path = pdf([["a.b axb a+b", "flow flowing flows"]])
        assert grep_pdf(path, r"a.b", regex=True, use_cache=False)["counts"] == {r"a.b": 3}
        assert grep_pdf(path, r"a\.b", regex=True, use_cache=False)["counts"] == {r"a\.b": 1}
        assert grep_pdf(path, r"flow(?:ing)?\b", regex=True, use_cache=False)["counts"] == {r"flow(?:ing)?\b": 2}
        assert grep_pdf(path, r"flow", regex=True, word=True, use_cache=False)["counts"] == {"flow": 1}

    def test_regex_mode_does_not_join_tokens(self, pdf):
        path = pdf([["carry-over carryover"]])
        assert grep_pdf(path, "carry-over", regex=True, use_cache=False)["counts"] == {"carry-over": 1}

    @pytest.mark.parametrize("term", ["(unclosed", "[a-", "*flow", ""])
    def test_bad_regex_raises_a_clear_error(self, pdf, term):
        path = pdf([["flow"]])
        with pytest.raises(BadRegexError) as excinfo:
            grep_pdf(path, [term], regex=True, use_cache=False)
        assert excinfo.value.code == "bad_regex"
        assert isinstance(excinfo.value, ValueError)
        if term:
            assert repr(term) in str(excinfo.value)

    def test_empty_term_list_is_an_error(self, pdf):
        with pytest.raises(BadRegexError):
            grep_pdf(pdf([["flow"]]), [], use_cache=False)

    def test_several_terms_count_separately(self, pdf):
        path = pdf([["flow flow pressure"], ["pressure"], ["neither"]])
        result = grep_pdf(path, ["flow", "pressure", "flow"], use_cache=False)
        assert result["terms"] == ["flow", "pressure"]
        assert result["counts"] == {"flow": 2, "pressure": 2}
        assert result["total_hits"] == 4
        assert result["pages_with_hits"] == 2
        assert result["pages"][0]["terms"] == {"flow": 2, "pressure": 1}
        assert result["pages"][0]["hits"] == 3
        assert result["pages"][1]["terms"] == {"pressure": 1}


# ---------------------------------------------------------------------------
# Snippets, limits, pages, order
# ---------------------------------------------------------------------------

FILLER = "filler " * 60  # 420 characters: farther apart than any window used below


class TestSnippets:
    def test_context_marks_the_match(self, pdf):
        path = pdf([["The flow rate rises"]])
        snippet = grep_pdf(path, "flow", context=4, use_cache=False)["pages"][0]["snippets"][0]
        assert snippet == {"text": "The [[flow]] rat", "terms": ["flow"]}

    def test_overlapping_windows_merge_into_one_snippet(self, pdf):
        path = pdf([["alpha flow beta gamma flow delta"]])
        page = grep_pdf(path, "flow", context=30, use_cache=False)["pages"][0]
        assert page["hits"] == 2
        assert len(page["snippets"]) == 1
        assert page["snippets"][0]["text"] == "alpha [[flow]] beta gamma [[flow]] delta"

    def test_distant_hits_stay_separate_snippets(self, pdf):
        path = pdf([[f"flow {FILLER}flow"]])
        page = grep_pdf(path, "flow", context=30, use_cache=False)["pages"][0]
        assert page["hits"] == 2
        assert len(page["snippets"]) == 2
        assert all(s["text"].count("[[") == 1 for s in page["snippets"])

    def test_overlapping_matches_of_two_terms_are_marked_once(self, pdf):
        path = pdf([["the flow rate"]])
        result = grep_pdf(path, ["flow", "flow rate"], context=4, use_cache=False)
        assert result["counts"] == {"flow": 1, "flow rate": 1}
        snippet = result["pages"][0]["snippets"][0]
        assert snippet["text"] == "the [[flow rate]]"
        assert snippet["terms"] == ["flow", "flow rate"]

    def test_max_hits_caps_snippets_but_not_counts(self, pdf):
        pages = [[f"flow {FILLER}flow {FILLER}flow"] for _ in range(4)]
        path = pdf(pages)
        result = grep_pdf(path, "flow", context=20, max_hits=5, use_cache=False)
        assert result["counts"] == {"flow": 12}
        assert result["total_hits"] == 12
        assert result["pages_with_hits"] == 4
        assert result["truncated"] is True
        assert sum(len(row["snippets"]) for row in result["pages"]) == 5
        assert [len(row["snippets"]) for row in result["pages"]] == [3, 2, 0, 0]
        assert [row["hits"] for row in result["pages"]] == [3, 3, 3, 3]

    def test_not_truncated_when_every_snippet_fits(self, pdf):
        path = pdf([[f"flow {FILLER}flow"]])
        result = grep_pdf(path, "flow", context=20, max_hits=2, use_cache=False)
        assert result["truncated"] is False
        assert len(result["pages"][0]["snippets"]) == 2

    def test_page_filter_limits_pages_and_counts(self, pdf):
        path = pdf([["flow"], ["flow"], ["flow flow"], ["flow"], ["flow"]])
        result = grep_pdf(path, "flow", pages=[(2, 3), (5, 5)], use_cache=False)
        assert [row["page"] for row in result["pages"]] == [2, 3, 5]
        assert result["counts"] == {"flow": 4}
        assert result["page_count"] == 5

    def test_page_filter_is_clipped_at_the_end_and_rejects_outside_ranges(self, pdf):
        path = pdf([["flow"], ["flow"]])
        assert grep_pdf(path, "flow", pages=[(2, 99)], use_cache=False)["counts"] == {"flow": 1}
        with pytest.raises(ValueError, match="out of range"):
            grep_pdf(path, "flow", pages=[(3, 4)], use_cache=False)

    def test_cache_off_page_filter_opens_the_pdf_once_per_run(self, pdf, monkeypatch):
        path = pdf([["flow"]] * 8)
        calls = []
        real = pdf_grep._extract_range
        monkeypatch.setattr(pdf_grep, "_extract_range", lambda p, a, b: calls.append((a, b)) or real(p, a, b))
        result = grep_pdf(path, "flow", pages=[(2, 4), (7, 7)], use_cache=False)
        assert calls == [(1, 4), (6, 7)]
        assert [row["page"] for row in result["pages"]] == [2, 3, 4, 7]

    def test_page_filter_applies_on_a_warm_cache_too(self, pdf):
        path = pdf([["flow"], ["flow"], ["flow"]])
        grep_pdf(path, "flow")
        result = grep_pdf(path, "flow", pages=[(3, 3)])
        assert result["cache"] == "hit"
        assert [row["page"] for row in result["pages"]] == [3]

    def test_order_score_ranks_by_hits_then_page(self, pdf):
        path = pdf([["flow"], ["flow flow flow"], ["flow flow"], ["flow flow flow"]])
        by_page = grep_pdf(path, "flow", use_cache=False)
        by_score = grep_pdf(path, "flow", order="score", use_cache=False)
        assert [row["page"] for row in by_page["pages"]] == [1, 2, 3, 4]
        assert [row["page"] for row in by_score["pages"]] == [2, 4, 3, 1]
        assert by_score["counts"] == by_page["counts"]

    def test_score_order_spends_the_snippet_budget_on_the_best_pages(self, pdf):
        path = pdf([["flow"], ["flow flow flow"]])
        result = grep_pdf(path, "flow", order="score", max_hits=1, context=3, use_cache=False)
        assert [row["page"] for row in result["pages"]] == [2, 1]
        assert len(result["pages"][0]["snippets"]) == 1
        assert result["pages"][1]["snippets"] == []
        assert result["truncated"] is True

    def test_unknown_order_is_rejected(self, pdf):
        with pytest.raises(ValueError, match="order"):
            grep_pdf(pdf([["flow"]]), "flow", order="alphabetical", use_cache=False)

    def test_page_is_physical_and_label_is_the_pdf_label(self, tmp_path):
        path = str(tmp_path / "labels.pdf")
        make_pdf(path, [["flow"], ["flow"], ["flow"], ["flow"]])
        doc = pymupdf.open(path)
        doc.set_page_labels([
            {"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
            {"startpage": 2, "prefix": "", "style": "D", "firstpagenum": 578},
        ])
        doc.saveIncr()
        doc.close()
        rows = grep_pdf(path, "flow", use_cache=False)["pages"]
        assert [(row["page"], row["label"]) for row in rows] == [(1, "i"), (2, "ii"), (3, "578"), (4, "579")]

    def test_label_is_empty_without_page_labels(self, pdf):
        row = grep_pdf(pdf([["flow"]]), "flow", use_cache=False)["pages"][0]
        assert row["label"] == ""

    def test_result_shape(self, pdf):
        result = grep_pdf(pdf([["flow"]]), "flow", use_cache=False)
        assert set(result) == {"key", "attachment_key", "title", "page_count", "terms", "counts", "total_hits",
                               "pages_with_hits", "truncated", "cache", "seconds", "pages"}
        assert set(result["pages"][0]) == {"page", "label", "hits", "terms", "snippets"}
        assert isinstance(result["seconds"], float)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_miss_then_hit_with_identical_results(self, pdf, cache_home):
        path = pdf([["flow"], ["carry-over flow"]])
        first = grep_pdf(path, "flow")
        second = grep_pdf(path, "flow")
        assert (first["cache"], second["cache"]) == ("miss", "hit")
        assert first["pages"] == second["pages"]
        assert first["counts"] == second["counts"] == {"flow": 2}
        assert len(list(cache_home.glob("*.json.gz"))) == 1

    def test_cache_off_reads_and_writes_nothing(self, pdf, cache_home):
        path = pdf([["flow"]])
        assert grep_pdf(path, "flow", use_cache=False)["cache"] == "off"
        assert not cache_home.exists()
        grep_pdf(path, "flow")
        assert grep_pdf(path, "flow", use_cache=False)["cache"] == "off"

    def test_touching_the_file_invalidates_the_entry(self, pdf, cache_home):
        path = pdf([["flow"]])
        assert grep_pdf(path, "flow")["cache"] == "miss"
        assert grep_pdf(path, "flow")["cache"] == "hit"
        stat = os.stat(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        assert grep_pdf(path, "flow")["cache"] == "miss"
        assert grep_pdf(path, "flow")["cache"] == "hit"
        assert len(list(cache_home.glob("*.json.gz"))) == 1  # the stale state was removed

    def test_changed_content_is_searched_afresh(self, pdf):
        path = pdf([["flow"]])
        assert grep_pdf(path, "flow")["total_hits"] == 1
        make_pdf(path, [["flow"], ["flow flow"]])
        result = grep_pdf(path, "flow")
        assert result["cache"] == "miss"
        assert result["total_hits"] == 3

    def test_normalizer_version_is_part_of_the_key(self, pdf, monkeypatch):
        path = pdf([["flow"]])
        assert grep_pdf(path, "flow")["cache"] == "miss"
        monkeypatch.setattr(pdf_grep, "NORMALIZER_VERSION", pdf_grep.NORMALIZER_VERSION + 1)
        assert grep_pdf(path, "flow")["cache"] == "miss"

    def test_corrupt_entry_is_a_miss(self, pdf, cache_home):
        path = pdf([["flow"]])
        grep_pdf(path, "flow")
        (entry,) = cache_home.glob("*.json.gz")
        entry.write_bytes(b"not gzip")
        result = grep_pdf(path, "flow")
        assert result["cache"] == "miss"
        assert result["total_hits"] == 1
        assert grep_pdf(path, "flow")["cache"] == "hit"

    def test_unwritable_cache_dir_does_not_fail_the_search(self, pdf, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the cache directory should go")
        monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
        result = grep_pdf(pdf([["flow"]]), "flow")
        assert result["cache"] == "miss"
        assert result["total_hits"] == 1

    def test_default_location_is_dot_cache(self, pdf, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CACHE_HOME")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
        grep_pdf(pdf([["flow"]]), "flow")
        assert len(list((tmp_path / "home" / ".cache" / "zotero-mcp" / "pagetext").glob("*.json.gz"))) == 1


# ---------------------------------------------------------------------------
# Process pool
# ---------------------------------------------------------------------------

class _InlinePool:
    """A pool that runs each job in the caller and records the page ranges it was given."""

    ranges: list = []

    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        _InlinePool.ranges.append(args[1:])
        future = Future()
        future.set_result(fn(*args))
        return future


class _BrokenPool:
    attempts = 0

    def __init__(self, *args, **kwargs):
        _BrokenPool.attempts += 1
        raise OSError("no semaphores in this sandbox")


class TestPool:
    @pytest.fixture
    def big_pdf(self, pdf):
        # 160 pages > POOL_MIN_PAGES; page n carries n % 3 hits of "flow"
        pages = [["flow " * (n % 3) + f"page {n}"] for n in range(1, 161)]
        return pdf(pages, "big.pdf", width=612)

    @staticmethod
    def expected():
        return sum(n % 3 for n in range(1, 161))

    @pytest.mark.parametrize("pages, pooled", [(10, False), (150, False), (151, True)])
    def test_pool_only_above_the_threshold(self, pdf, monkeypatch, pages, pooled):
        _InlinePool.ranges = []
        monkeypatch.setattr(pdf_grep, "ProcessPoolExecutor", _InlinePool)
        result = grep_pdf(pdf([["flow"]] * pages, width=612), "flow", use_cache=False)
        assert result["total_hits"] == pages
        assert bool(_InlinePool.ranges) is pooled

    def test_cache_off_page_filter_pools_only_a_large_selection(self, big_pdf, monkeypatch):
        _InlinePool.ranges = []
        monkeypatch.setattr(pdf_grep, "ProcessPoolExecutor", _InlinePool)
        grep_pdf(big_pdf, "flow", pages=[(1, 20)], use_cache=False)
        assert _InlinePool.ranges == []
        result = grep_pdf(big_pdf, "flow", pages=[(1, 155)], use_cache=False)
        assert _InlinePool.ranges
        assert result["pages"][-1]["page"] <= 155

    def test_large_pdf_is_split_across_workers_in_page_order(self, big_pdf, monkeypatch):
        _InlinePool.ranges = []
        monkeypatch.setattr(pdf_grep, "ProcessPoolExecutor", _InlinePool)
        result = grep_pdf(big_pdf, "flow", use_cache=False, jobs=4)
        assert result["total_hits"] == self.expected()
        assert len(_InlinePool.ranges) == 4
        assert _InlinePool.ranges[0][0] == 0 and _InlinePool.ranges[-1][1] == 160
        assert all(a[1] == b[0] for a, b in zip(_InlinePool.ranges, _InlinePool.ranges[1:]))
        assert [row["page"] for row in result["pages"]][:4] == [1, 2, 4, 5]

    def test_pool_failure_falls_back_to_serial_with_correct_counts(self, big_pdf, monkeypatch):
        _BrokenPool.attempts = 0
        monkeypatch.setattr(pdf_grep, "ProcessPoolExecutor", _BrokenPool)
        result = grep_pdf(big_pdf, "flow", use_cache=False)
        assert _BrokenPool.attempts == 1  # the pool was tried, then serial took over
        assert result["counts"] == {"flow": self.expected()}
        assert result["page_count"] == 160

    def test_worker_error_falls_back_to_serial(self, big_pdf, monkeypatch):
        class Dying(_InlinePool):
            def submit(self, fn, *args):
                future = Future()
                future.set_exception(RuntimeError("worker died"))
                return future

        monkeypatch.setattr(pdf_grep, "ProcessPoolExecutor", Dying)
        result = grep_pdf(big_pdf, "flow", use_cache=False)
        assert result["counts"] == {"flow": self.expected()}

    def test_real_process_pool_matches_a_serial_pass(self, big_pdf):
        try:  # the fallback would hide a pool that never ran, so call the pool path directly
            direct = pdf_grep._extract_parallel(big_pdf, 160, 2)
        except (OSError, ImportError) as exc:
            pytest.skip(f"this sandbox cannot start a process pool: {exc}")
        assert direct == pdf_grep._extract_range(big_pdf, 0, 160)
        pooled = grep_pdf(big_pdf, "flow", use_cache=False, jobs=2)
        assert pooled["counts"] == {"flow": self.expected()}
        assert pooled["pages"][0]["page"] == 1 and pooled["pages"][-1]["page"] == 160

    def test_a_cache_written_by_the_pool_serves_the_next_call(self, big_pdf):
        assert grep_pdf(big_pdf, "flow", jobs=2)["cache"] == "miss"
        assert grep_pdf(big_pdf, "flow")["cache"] == "hit"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

class TestMarkdown:
    def test_renders_summary_pages_and_snippets(self, pdf):
        data = grep_pdf(pdf([["The flow rate rises"], ["pressure and flow"]]), ["flow", "pressure"], context=10,
                        use_cache=False)
        data["title"] = "Gas turbine handbook"
        text = format_grep_markdown(data)
        assert text.startswith("# grep: Gas turbine handbook\n")
        assert "3 hits on 2 of 2 pages (flow 2, pressure 1)" in text
        assert "## p. 1: 1 hits (flow 1)" in text
        assert "## p. 2: 2 hits (flow 1, pressure 1)" in text
        assert "[[flow]]" in text
        assert "cache off" in text

    def test_shows_a_page_label_only_when_it_differs(self):
        data = {"title": None, "key": "ABCD1234", "page_count": 9, "counts": {"x": 2}, "total_hits": 2,
                "pages_with_hits": 2, "truncated": True, "cache": "hit", "seconds": 0.1, "pages": [
                    {"page": 3, "label": "3", "hits": 1, "terms": {"x": 1}, "snippets": [{"text": "a [[x]]", "terms": ["x"]}]},
                    {"page": 4, "label": "578", "hits": 1, "terms": {"x": 1}, "snippets": []},
                ]}
        text = format_grep_markdown(data)
        assert "# grep: ABCD1234" in text
        assert "## p. 3: 1 hits (x 1)" in text
        assert "## p. 4 [578]: 1 hits (x 1)" in text
        assert "cut by max-hits" in text
