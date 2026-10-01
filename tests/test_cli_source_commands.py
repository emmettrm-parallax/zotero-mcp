"""`zotero-cli grep`, `sections`, `tables` and `index`, and the PDF key resolver under them.

The engines behind these commands (`pdf_grep`, `pdf_sections`, `pdf_tables`,
`source_index`, `index_slice`, `index_grep`) are separate modules. These tests replace them with fakes injected
through `sys.modules`, so what is pinned here is the wiring alone: which arguments the
CLI hands each engine, what identity fields it adds to the result, and the
shape of the JSON envelope and its error codes.

The last class pins a bug that sits under every PDF command. PyMuPDF prints its
`import fitz` deprecation warning on stdout, so `--json read` and
`--json layout` output did not parse as JSON.
"""

import io
import json
import os
import re
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zotero_mcp import cli_standalone, pdf_source
from zotero_mcp.cli_json import CliError
from zotero_mcp.cli_standalone import build_parser

REPO = Path(__file__).resolve().parent.parent

PARENT = "PARENT01"
ATTACH = "ATTACH01"
PDF_PATH = "/library/storage/ATTACH01/paper.pdf"
TITLE = "A Paper About Turbines"

GREP_RESULT = {
    "page_count": 40,
    "terms": ["pressure ratio"],
    "counts": {"pressure ratio": 3},
    "total_hits": 3,
    "pages_with_hits": 2,
    "truncated": False,
    "cache": "miss",
    "seconds": 0.5,
    "pages": [
        {"page": 4, "label": "4", "hits": 2, "terms": {"pressure ratio": 2},
         "snippets": [{"text": "the [[pressure ratio]] was", "terms": ["pressure ratio"]}]},
        {"page": 9, "label": "9", "hits": 1, "terms": {"pressure ratio": 1},
         "snippets": [{"text": "a [[pressure ratio]] of", "terms": ["pressure ratio"]}]},
    ],
}

SECTIONS_RESULT = {
    "page_count": 40,
    "source": "outline",
    "scope": [1, 40],
    "sections": [
        {"id": "S01", "title": "Intro", "path": "Intro", "level": 1,
         "start_page": 1, "end_page": 6, "kind": "outline"},
    ],
}

TABLES_RESULT = {
    "strategy": "lines",
    "pages": [
        {"page": 17, "tables": [
            {"id": "p17-t1", "bbox": [0.1, 0.2, 0.8, 0.3], "rect_arg": "0.1000,0.2000,0.8000,0.3000",
             "header": ["Alloy", "Temp (C)"], "rows": [["N07001", "760"], ["N07002", None]],
             "caption": "Table 2. Test alloys"},
        ]},
        {"page": 18, "tables": []},
    ],
}

PUSH_RESULT = {
    "item_key": PARENT, "note_keys": ["NOTE0001", "NOTE0002"], "parts": 2, "chars": 210000,
    "created": 2, "updated": 0, "trashed": 0, "dry_run": False,
    "counts": {"facts": 1200, "sections": 9},
}

SHOW_RESULT = {
    "item_key": PARENT, "note_keys": ["NOTE0001"], "parts": 1,
    "index": {"schema": "source-index/v1", "facts": []},
}


INDEX_KINDS = ("facts", "vocabulary", "tables_figures", "equations", "gaps")


def fact_record(number):
    """One fact of a synthetic index, with the fields a lead line shows."""
    return {"id": f"F{number:04d}", "page": number, "kind": "text", "quantity": "leakage rate",
            "value": "5.34", "unit": "g/s", "condition": "at 3 bar", "ref": "Table 2",
            "section_id": "S01"}


def make_index(facts=0, vocabulary=0, *, title="Windback Seals", year="2018",
               attachment="ATTACH01", page_count=15):
    """A small synthetic index with *facts* facts and *vocabulary* vocabulary entries."""
    return {
        "schema": "source-index/v1",
        "header": {"attachment_key": attachment, "title": title, "year": year,
                   "page_count": page_count},
        "sections": [{"id": "S01", "title": "Intro"}],
        "vocabulary": [{"term": f"term {n}", "kind": "term", "symbol": None, "variants": [],
                        "pages": [1]} for n in range(vocabulary)],
        "facts": [fact_record(n) for n in range(1, facts + 1)],
        "tables_figures": [], "equations": [], "gaps": [],
    }


def indexed(key, **kwargs):
    """What `show_index` returns for *key*: a synthetic index under the note envelope."""
    return {"item_key": key, "note_keys": [f"NOTE-{key}"], "parts": 1,
            "index": make_index(**kwargs)}


class Fakes(SimpleNamespace):
    """What the fake engines were called with, and what they return."""


def _install(monkeypatch, name, module):
    """Make `from zotero_mcp import <name>` find *module*.

    Both places are set. A real module that an earlier test already imported
    sits as an attribute on the package, and `from package import name` reads
    that attribute before it looks in `sys.modules`.
    """
    import zotero_mcp

    monkeypatch.setitem(sys.modules, f"zotero_mcp.{name}", module)
    monkeypatch.setattr(zotero_mcp, name, module, raising=False)


@pytest.fixture
def fakes(monkeypatch):
    calls = Fakes(resolved=[], parent_keys=[], grep=[], sections=[], tables=[], push=[],
                  show=[], slice=[], page_counts=[], validate=[], errors_for_validate=[],
                  pdf_pages=1200, index_pages=40,
                  # index_grep, show_indexes and list_items_with_tag
                  index_grep=[], parse_terms=[], check_terms=[], filters=[],
                  show_indexes=[], tag_calls=[], tagged=[], search_indexes={}, parent_map={},
                  expanded=["wind-back"], broad=[])

    grep_mod = types.ModuleType("zotero_mcp.pdf_grep")

    def grep_pdf(path, terms, **kwargs):
        calls.grep.append((path, terms, kwargs))
        return dict(GREP_RESULT)

    grep_mod.grep_pdf = grep_pdf
    grep_mod.format_grep_markdown = lambda data: f"GREP-MD {data['key']} {data['total_hits']}"

    sections_mod = types.ModuleType("zotero_mcp.pdf_sections")

    def sections_for_pdf(path, **kwargs):
        calls.sections.append((path, kwargs))
        return dict(SECTIONS_RESULT)

    sections_mod.sections_for_pdf = sections_for_pdf
    sections_mod.format_sections_markdown = lambda data: f"SECTIONS-MD {data['key']}"

    tables_mod = types.ModuleType("zotero_mcp.pdf_tables")

    def tables_for_pdf(path, **kwargs):
        calls.tables.append((path, kwargs))
        return dict(TABLES_RESULT, strategy=kwargs["strategy"])

    tables_mod.tables_for_pdf = tables_for_pdf
    tables_mod.format_tables_markdown = lambda data: f"TABLES-MD {data['attachment_key']}"

    slice_mod = types.ModuleType("zotero_mcp.index_slice")

    def filter_index_pages(index, pages):
        """Raises what the real slice raises; otherwise tags the index with the pages kept."""
        calls.slice.append((index, pages))
        if not pages or min(pages) < 1 or max(pages) > calls.index_pages:
            raise CliError(f"pages outside 1-{calls.index_pages}", code="bad_pages")
        return {**index, "pages_kept": pages}

    slice_mod.filter_index_pages = filter_index_pages

    index_mod = types.ModuleType("zotero_mcp.source_index")

    def validate_index(obj):
        calls.validate.append(obj)
        return list(calls.errors_for_validate)

    def push_index(parent_key, index, **kwargs):
        calls.push.append((parent_key, index, kwargs))
        return dict(PUSH_RESULT)

    def show_index(parent_key, **kwargs):
        calls.show.append((parent_key, kwargs))
        return dict(SHOW_RESULT)

    def show_indexes(parent_keys, *, ctx):
        """One entry per key that the test set up; a key it left out is missing from the result."""
        calls.show_indexes.append((list(parent_keys), ctx))
        return {key: calls.search_indexes[key] for key in parent_keys if key in calls.search_indexes}

    def list_items_with_tag(tag, **kwargs):
        calls.tag_calls.append((tag, kwargs))
        return list(calls.tagged)

    index_mod.validate_index = validate_index
    index_mod.push_index = push_index
    index_mod.show_index = show_index
    index_mod.show_indexes = show_indexes
    index_mod.list_items_with_tag = list_items_with_tag

    grep_index_mod = types.ModuleType("zotero_mcp.index_grep")

    def parse_terms(values, *, regex):
        """The documented rule: comma split unless regex, strip, drop empties and casefold duplicates."""
        calls.index_grep.append("parse_terms")
        calls.parse_terms.append((list(values), regex))
        parts = list(values) if regex else [p.strip() for v in values for p in v.split(",")]
        terms, seen = [], set()
        for part in parts:
            if part and part.casefold() not in seen:
                seen.add(part.casefold())
                terms.append(part)
        if not terms:
            raise CliError("no terms to search for", code="bad_grep")
        return terms

    def check_terms(terms, *, regex):
        calls.index_grep.append("check_terms")
        calls.check_terms.append((list(terms), regex))
        for term in terms if regex else ():
            try:
                re.compile(term)
            except re.error as exc:
                raise CliError(f"Bad regex {term!r}: {exc}", code="bad_regex") from exc

    def apply_filters(index, *, pages=None, terms=None, regex=False, expand=False, fields="full",
                      limit=40):
        """Keeps every entry up to the limit and reports the counts in the documented shape."""
        calls.index_grep.append("apply_filters")
        calls.filters.append((index, dict(pages=pages, terms=terms, regex=regex, expand=expand,
                                          fields=fields, limit=limit)))
        if pages and max(pages) > calls.index_pages:
            raise CliError(f"pages outside 1-{calls.index_pages}", code="bad_pages")
        subset, total, returned = dict(index), {}, {}
        for kind in INDEX_KINDS:
            entries = index.get(kind) or []
            kept = entries[:limit] if limit else entries
            total[kind], returned[kind] = len(entries), len(kept)
            if kind in index:
                subset[kind] = kept
        return subset, {
            "terms": list(terms or []), "regex": regex, "expand": expand, "fields": fields,
            "limit": limit, "pages": pages,
            "expanded_terms": list(calls.expanded), "expanded_total": len(calls.expanded),
            "expanded_symbols": [], "broad_terms": list(calls.broad),
            "term_counts": {term: 1 for term in terms or []},
            "total": total, "matched": dict(total), "returned": returned,
            "truncated": {kind: total[kind] - returned[kind] for kind in INDEX_KINDS},
        }

    grep_index_mod.parse_terms = parse_terms
    grep_index_mod.check_terms = check_terms
    grep_index_mod.apply_filters = apply_filters

    for name, module in (("pdf_grep", grep_mod), ("pdf_sections", sections_mod),
                         ("pdf_tables", tables_mod), ("index_slice", slice_mod),
                         ("source_index", index_mod), ("index_grep", grep_index_mod)):
        _install(monkeypatch, name, module)

    @contextmanager
    def resolved_pdf(key, ctx):
        calls.resolved.append(key)
        yield pdf_source.ResolvedPdf(PARENT, ATTACH, PDF_PATH, TITLE, False)

    def parent_key(key):
        calls.parent_keys.append(key)
        return calls.parent_map.get(key, PARENT)

    def pdf_page_count(path):
        calls.page_counts.append(path)
        return calls.pdf_pages

    monkeypatch.setattr(pdf_source, "resolved_pdf", resolved_pdf)
    monkeypatch.setattr(pdf_source, "parent_key", parent_key)
    monkeypatch.setattr(cli_standalone, "_pdf_page_count", pdf_page_count)
    monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
    monkeypatch.setattr(cli_standalone, "_keep_pymupdf_off_stdout", lambda: None)
    return calls


def run_cli(monkeypatch, capsys, *argv):
    """Run `zotero-cli *argv` in process; return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["zotero-cli", *argv])
    code = 0
    try:
        cli_standalone.main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(monkeypatch, capsys, *argv):
    code, out, _err = run_cli(monkeypatch, capsys, *argv)
    return code, json.loads(out)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_grep_defaults(self):
        args = build_parser().parse_args(["grep", "KEY1", "pressure ratio", "mach"])
        assert args.command == "grep"
        assert args.key == "KEY1"
        assert args.terms == ["pressure ratio", "mach"]
        assert (args.regex, args.word, args.no_cache) == (False, False, False)
        assert (args.pages, args.context, args.max_hits) == ("all", 300, 200)
        assert (args.order, args.jobs) == ("page", None)

    def test_grep_needs_a_term(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["grep", "KEY1"])

    def test_grep_order_is_page_or_score(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["grep", "KEY1", "x", "--order", "alpha"])

    def test_sections_defaults(self):
        args = build_parser().parse_args(["sections", "KEY1"])
        assert args.command == "sections"
        assert (args.pages, args.max_level, args.chunk_pages) == ("all", 2, 8)
        assert args.inventory is False

    def test_tables_defaults(self):
        args = build_parser().parse_args(["tables", "KEY1", "--pages", "17"])
        assert args.command == "tables"
        assert (args.key, args.pages, args.strategy) == ("KEY1", "17", "lines")

    def test_tables_needs_pages(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["tables", "KEY1"])

    def test_tables_strategy_is_lines_or_text(self):
        args = build_parser().parse_args(["tables", "KEY1", "--pages", "3", "--strategy", "text"])
        assert args.strategy == "text"
        with pytest.raises(SystemExit):
            build_parser().parse_args(["tables", "KEY1", "--pages", "3", "--strategy", "grid"])

    def test_index_push_needs_a_source_file(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["index", "push", "KEY1"])

    def test_index_needs_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["index"])

    def test_index_push_flags(self):
        args = build_parser().parse_args(
            ["index", "push", "KEY1", "--from", "-", "--replace", "--tags", "a,b", "--dry-run"])
        assert (args.subcommand, args.key, args.from_file) == ("push", "KEY1", "-")
        assert (args.replace, args.tags, args.dry_run) == (True, "a,b", True)

    def test_index_show_flags(self):
        args = build_parser().parse_args(["index", "show", "KEY1", "--section", "S03"])
        assert (args.subcommand, args.key, args.section) == ("show", "KEY1", "S03")
        assert args.pages is None

    def test_index_show_takes_pages(self):
        args = build_parser().parse_args(
            ["index", "show", "KEY1", "--section", "S03", "--pages", "665-666"])
        assert (args.section, args.pages) == ("S03", "665-666")

    def test_index_push_tags_go_on_the_parent_item(self, capsys):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["index", "push", "--help"])
        help_text = " ".join(capsys.readouterr().out.split())
        assert "Comma-separated tags to add to the parent item" in help_text

    @pytest.mark.parametrize("argv", [
        ["--json", "index", "show", "KEY1"],
        ["index", "--json", "show", "KEY1"],
        ["index", "show", "KEY1", "--json"],
        ["--json", "index", "push", "KEY1", "--from", "f.json"],
        ["index", "push", "KEY1", "--from", "f.json", "--json"],
    ])
    def test_json_flag_works_anywhere(self, argv):
        assert build_parser().parse_args(argv).json_out is True

    def test_json_flag_before_the_command_survives_the_leaf_default(self):
        """The leaf's own --json must not write False over the global one."""
        args = build_parser().parse_args(["--json", "index", "show", "KEY1"])
        assert args.json_out is True
        assert build_parser().parse_args(["index", "show", "KEY1"]).json_out is False

    def test_every_new_command_has_a_handler(self):
        for name in ("grep", "sections", "tables", "index"):
            assert name in cli_standalone._CMD_MAP

    def test_schema_doc_names_the_new_commands(self):
        for line in ("  grep ", "  sections ", "  tables ", "  index push ", "  index show "):
            assert line in cli_standalone.JSON_SCHEMA_DOC
        assert "data.pages[].tables[] -- header, rows, caption, rect_arg" in \
            cli_standalone.JSON_SCHEMA_DOC
        for code in ("no_pdf_attachment", "bad_regex", "bad_pages", "no_index", "index_exists",
                     "invalid_index"):
            assert code in cli_standalone.JSON_SCHEMA_DOC


class TestIndexFilterParsers:
    def test_show_filter_flags_are_absent_by_default(self):
        """None, not the working default, so that the command sees what was given."""
        args = build_parser().parse_args(["index", "show", "KEY1"])
        assert (args.grep, args.regex, args.fields, args.expand, args.limit) == \
            (None, False, None, False, None)

    def test_show_filter_flags_parse(self):
        args = build_parser().parse_args(
            ["index", "show", "KEY1", "--grep", "a,b", "--grep", "c", "--regex", "--fields", "lead",
             "--expand", "--limit", "5"])
        assert args.grep == ["a,b", "c"]
        assert (args.regex, args.fields, args.expand, args.limit) == (True, "lead", True, 5)

    @pytest.mark.parametrize("choice", ["lead", "full"])
    def test_show_fields_takes_lead_or_full(self, choice):
        args = build_parser().parse_args(["index", "show", "KEY1", "--fields", choice])
        assert args.fields == choice

    def test_show_fields_refuses_any_other_value(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["index", "show", "KEY1", "--fields", "short"])
        assert exc.value.code == 2

    def test_search_defaults(self):
        args = build_parser().parse_args(["index", "search", "leakage"])
        assert (args.command, args.subcommand, args.terms) == ("index", "search", ["leakage"])
        assert (args.items, args.tag, args.fields) == \
            (None, "status/indexed,status/index-failed-gate", "lead")
        assert (args.expand, args.regex, args.limit, args.max_items) == (False, False, 10, 10)

    def test_search_flags_parse(self):
        args = build_parser().parse_args(
            ["index", "search", "leakage,seal", "wind-back", "--items", "K1,K2", "--tag", "x/y",
             "--fields", "full", "--expand", "--limit", "3", "--max-items", "4", "--regex"])
        assert args.terms == ["leakage,seal", "wind-back"]
        assert (args.items, args.tag, args.fields) == ("K1,K2", "x/y", "full")
        assert (args.expand, args.regex, args.limit, args.max_items) == (True, True, 3, 4)

    def test_search_needs_a_term(self):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["index", "search"])
        assert exc.value.code == 2

    def test_search_fields_takes_lead_or_full(self):
        assert build_parser().parse_args(["index", "search", "x", "--fields", "full"]).fields == "full"
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["index", "search", "x", "--fields", "short"])
        assert exc.value.code == 2

    @pytest.mark.parametrize("argv", [
        ["index", "show", "KEY1", "--limit", "-1"],
        ["index", "show", "KEY1", "--limit", "many"],
        ["index", "search", "x", "--limit", "-1"],
        ["index", "search", "x", "--limit", "1.5"],
        ["index", "search", "x", "--max-items", "-2"],
        ["index", "search", "x", "--max-items", "lots"],
    ])
    def test_limit_and_max_items_refuse_a_negative_or_non_integer_value(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(argv)
        assert exc.value.code == 2
        assert "whole number from 0 up" in capsys.readouterr().err

    def test_zero_means_no_cap_and_is_accepted(self):
        assert build_parser().parse_args(["index", "show", "KEY1", "--limit", "0"]).limit == 0
        args = build_parser().parse_args(["index", "search", "x", "--limit", "0", "--max-items", "0"])
        assert (args.limit, args.max_items) == (0, 0)

    @pytest.mark.parametrize("argv", [
        ["--json", "index", "search", "x"],
        ["index", "--json", "search", "x"],
        ["index", "search", "x", "--json"],
        ["index", "search", "--json", "x", "y"],
    ])
    def test_json_flag_works_anywhere_on_search(self, argv):
        assert build_parser().parse_args(argv).json_out is True

    def test_json_flag_before_the_command_survives_the_search_leaf_default(self):
        assert build_parser().parse_args(["--json", "index", "search", "x"]).json_out is True
        assert build_parser().parse_args(["index", "search", "x"]).json_out is False

    def test_schema_doc_names_index_search_the_filter_and_bad_grep(self):
        doc = cli_standalone.JSON_SCHEMA_DOC
        assert "  index search " in doc
        assert ("with --grep, --fields or --limit also data.filter{} -- "
                "terms, matched{}, truncated{}") in doc
        assert ("data.items[] -- hits per indexed item, most facts first; "
                "data.no_hits[], data.skipped[]") in doc
        assert "bad_grep" in doc

    def test_the_limit_defaults_match_index_grep(self):
        """Skipped until `index_grep` (F1) is on the branch; the merge then compares the copies."""
        index_grep = pytest.importorskip("zotero_mcp.index_grep")
        assert cli_standalone._INDEX_SHOW_LIMIT == index_grep.SHOW_LIMIT
        assert build_parser().parse_args(["index", "search", "x"]).limit == index_grep.SEARCH_LIMIT


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------

class TestGrep:
    def test_envelope_carries_identity_and_the_engine_result(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "grep", ATTACH, "pressure ratio")
        assert code == 0
        assert (body["ok"], body["command"], body["schema"]) == (True, "grep", 1)
        data = body["data"]
        assert (data["key"], data["attachment_key"], data["title"]) == (PARENT, ATTACH, TITLE)
        assert data["counts"] == {"pressure ratio": 3}
        assert data["pages"][0]["snippets"][0]["text"] == "the [[pressure ratio]] was"
        assert list(data)[:3] == ["key", "attachment_key", "title"]
        assert fakes.resolved == [ATTACH]

    def test_engine_gets_the_documented_defaults(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "pressure ratio", "mach")
        [(path, terms, kwargs)] = fakes.grep
        assert path == PDF_PATH
        assert terms == ["pressure ratio", "mach"]
        assert kwargs == {"regex": False, "word": False, "pages": None, "context": 300,
                          "max_hits": 200, "order": "page", "use_cache": True, "jobs": None}

    def test_every_flag_reaches_the_engine(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "grep", "KEY1", "a.b", "c", "--regex", "--word",
                 "--pages", "3-5,9", "--context", "50", "--max-hits", "7",
                 "--order", "score", "--no-cache", "--jobs", "2", "--json")
        [(_path, terms, kwargs)] = fakes.grep
        assert terms == ["a.b", "c"]
        assert kwargs == {"regex": True, "word": True, "pages": [3, 4, 5, 9], "context": 50,
                          "max_hits": 7, "order": "score", "use_cache": False, "jobs": 2}

    def test_the_cli_owns_the_identity_fields(self, fakes, monkeypatch, capsys):
        """An engine that returns its own `key` or `title` must not win."""
        result = dict(GREP_RESULT, key="WRONG", title="Wrong", extra="kept")
        with patch.object(sys.modules["zotero_mcp.pdf_grep"], "grep_pdf", return_value=result):
            _code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x")
        assert body["data"]["key"] == PARENT
        assert body["data"]["title"] == TITLE
        assert body["data"]["extra"] == "kept"

    def test_markdown_mode_prints_the_engines_markdown(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "grep", "KEY1", "x")
        assert code == 0
        assert out.strip() == f"GREP-MD {PARENT} 3"

    def test_a_bad_regex_fails_before_the_pdf_is_fetched(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "ok", "(", "--regex")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_regex")
        assert "(" in body["error"]["message"]
        assert fakes.resolved == [] and fakes.grep == []

    def test_a_bracket_is_fine_without_regex(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "(")
        assert code == 0 and body["ok"] is True

    def test_a_regex_error_from_the_engine_keeps_its_code(self, fakes, monkeypatch, capsys):
        with patch.object(sys.modules["zotero_mcp.pdf_grep"], "grep_pdf",
                          side_effect=re.error("nothing to repeat")):
            code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x")
        assert code == 1
        assert body["error"]["code"] == "bad_regex"

    def test_bad_pages_fail_before_the_pdf_is_fetched(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x", "--pages", "0")
        assert code == 1
        assert body["error"]["code"] == "bad_pages"
        assert fakes.resolved == []

    def test_pages_are_checked_against_the_pdf_before_the_engine_runs(self, fakes, monkeypatch,
                                                                       capsys):
        run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x", "--pages", "1-1200")
        assert fakes.page_counts == [PDF_PATH]
        assert len(fakes.grep) == 1

    def test_a_page_past_the_end_is_bad_pages(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x",
                              "--pages", "3,1201")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_pages")
        assert "1201" in body["error"]["message"] and "1200" in body["error"]["message"]
        assert fakes.grep == []

    def test_all_pages_need_no_page_count(self, fakes, monkeypatch, capsys):
        code, _body = run_json(monkeypatch, capsys, "--json", "grep", "KEY1", "x")
        assert code == 0
        assert fakes.page_counts == []

    def test_a_key_with_no_pdf_reports_it(self, fakes, monkeypatch, capsys):
        @contextmanager
        def no_pdf(key, ctx):
            raise CliError(f"No PDF attachment found for item: {key}", code="no_pdf_attachment")
            yield  # pragma: no cover

        monkeypatch.setattr(pdf_source, "resolved_pdf", no_pdf)
        code, body = run_json(monkeypatch, capsys, "--json", "grep", "NOPDF001", "x")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "no_pdf_attachment")
        assert fakes.grep == []


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

class TestSections:
    def test_envelope_and_defaults(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "sections", ATTACH)
        assert code == 0
        assert (body["ok"], body["command"]) == (True, "sections")
        data = body["data"]
        assert (data["key"], data["attachment_key"], data["title"]) == (PARENT, ATTACH, TITLE)
        assert data["source"] == "outline" and data["scope"] == [1, 40]
        assert data["sections"][0]["id"] == "S01"
        assert fakes.sections == [(PDF_PATH, {"pages": None, "max_level": 2,
                                              "chunk_pages": 8, "inventory": False})]

    def test_every_flag_reaches_the_engine(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "sections", "KEY1", "--pages", "665-689",
                 "--max-level", "1", "--chunk-pages", "5", "--inventory", "--json")
        [(_path, kwargs)] = fakes.sections
        assert kwargs == {"pages": list(range(665, 690)), "max_level": 1,
                          "chunk_pages": 5, "inventory": True}

    def test_the_inventory_the_engine_returns_is_kept(self, fakes, monkeypatch, capsys):
        inventory = [{"section_id": "S01", "tables": ["Table 1"], "figures": [],
                      "equations": ["(1)"], "unnumbered_equations": 2}]
        result = dict(SECTIONS_RESULT, inventory=inventory)
        with patch.object(sys.modules["zotero_mcp.pdf_sections"], "sections_for_pdf",
                          return_value=result):
            _code, body = run_json(monkeypatch, capsys, "--json", "sections", "KEY1",
                                   "--inventory")
        assert body["data"]["inventory"] == inventory

    def test_markdown_mode_prints_the_engines_markdown(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "sections", "KEY1")
        assert code == 0
        assert out.strip() == f"SECTIONS-MD {PARENT}"

    def test_bad_pages_fail_before_the_pdf_is_fetched(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "sections", "KEY1", "--pages", "x")
        assert code == 1
        assert body["error"]["code"] == "bad_pages"
        assert fakes.resolved == []

    def test_a_page_past_the_end_is_bad_pages(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "sections", "KEY1",
                              "--pages", "1190-1201")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_pages")
        assert fakes.page_counts == [PDF_PATH]
        assert fakes.sections == []

    def test_the_last_page_is_in_range(self, fakes, monkeypatch, capsys):
        code, _body = run_json(monkeypatch, capsys, "--json", "sections", "KEY1",
                               "--pages", "1200")
        assert code == 0
        assert fakes.sections[0][1]["pages"] == [1200]


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

class TestTables:
    def test_envelope_carries_the_attachment_key_and_the_engine_result(self, fakes, monkeypatch,
                                                                       capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "tables", ATTACH, "--pages", "17-18")
        assert code == 0
        assert (body["ok"], body["command"], body["schema"]) == (True, "tables", 1)
        data = body["data"]
        assert list(data) == ["attachment_key", "strategy", "pages"]
        assert data["attachment_key"] == ATTACH
        assert data["strategy"] == "lines"
        assert data["pages"] == TABLES_RESULT["pages"]
        assert data["pages"][0]["tables"][0]["rows"][1] == ["N07002", None]
        assert data["pages"][1] == {"page": 18, "tables": []}
        assert fakes.resolved == [ATTACH]

    def test_the_attachment_key_is_the_resolved_pdfs(self, fakes, monkeypatch, capsys):
        """An item key in, the PDF's own key out."""
        _code, body = run_json(monkeypatch, capsys, "--json", "tables", PARENT, "--pages", "17")
        assert body["data"]["attachment_key"] == ATTACH

    def test_engine_gets_the_documented_defaults(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "tables", "KEY1", "--pages", "17")
        assert fakes.tables == [(PDF_PATH, {"pages": [17], "strategy": "lines"})]

    def test_every_flag_reaches_the_engine(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "tables", "KEY1", "--pages", "13,17-18",
                              "--strategy", "text", "--json")
        assert code == 0
        assert fakes.tables == [(PDF_PATH, {"pages": [13, 17, 18], "strategy": "text"})]
        assert body["data"]["strategy"] == "text"

    def test_all_pages_reach_the_engine_as_none(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "tables", "KEY1", "--pages", "all")
        assert fakes.tables[0][1]["pages"] is None
        assert fakes.page_counts == []

    def test_markdown_mode_prints_the_engines_markdown(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "tables", "KEY1", "--pages", "17")
        assert code == 0
        assert out.strip() == f"TABLES-MD {ATTACH}"

    def test_pages_are_required(self, fakes, monkeypatch, capsys):
        code, out, err = run_cli(monkeypatch, capsys, "--json", "tables", "KEY1")
        assert code == 2
        assert "--pages" in err
        assert fakes.resolved == [] and fakes.tables == []

    def test_an_unparseable_page_list_fails_before_the_pdf_is_fetched(self, fakes, monkeypatch,
                                                                       capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "tables", "KEY1", "--pages", "0")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_pages")
        assert fakes.resolved == [] and fakes.tables == []

    def test_a_page_past_the_end_is_bad_pages(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "tables", "KEY1",
                              "--pages", "1201")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_pages")
        assert "1201" in body["error"]["message"]
        assert fakes.page_counts == [PDF_PATH]
        assert fakes.tables == []

    def test_a_key_with_no_pdf_reports_it(self, fakes, monkeypatch, capsys):
        @contextmanager
        def no_pdf(key, ctx):
            raise CliError(f"No PDF attachment found for item: {key}", code="no_pdf_attachment")
            yield  # pragma: no cover

        monkeypatch.setattr(pdf_source, "resolved_pdf", no_pdf)
        code, body = run_json(monkeypatch, capsys, "--json", "tables", "NOPDF001", "--pages", "1")
        assert code == 1
        assert body["error"]["code"] == "no_pdf_attachment"
        assert fakes.tables == []


class TestCheckPages:
    """The range check that grep, sections and tables share."""

    @pytest.mark.parametrize("pages", [[], [0], [-3, 4]])
    def test_an_empty_list_or_a_page_below_one_never_opens_the_pdf(self, fakes, pages):
        with pytest.raises(CliError) as caught:
            cli_standalone._check_pages(pages, PDF_PATH)
        assert caught.value.code == "bad_pages"
        assert fakes.page_counts == []

    def test_a_page_past_the_count_is_refused(self, fakes):
        fakes.pdf_pages = 40
        cli_standalone._check_pages([1, 40], PDF_PATH)
        with pytest.raises(CliError) as caught:
            cli_standalone._check_pages([1, 41], PDF_PATH)
        assert caught.value.code == "bad_pages"
        assert "41" in str(caught.value) and "40" in str(caught.value)

    def test_none_is_every_page(self, fakes):
        cli_standalone._check_pages(None, PDF_PATH)
        assert fakes.page_counts == []


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

@pytest.fixture
def index_file(tmp_path):
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"schema": "source-index/v1", "facts": []}), encoding="utf-8")
    return path


class TestIndexPush:
    def test_pushes_to_the_parent_of_the_key_it_was_given(self, fakes, monkeypatch, capsys,
                                                          index_file):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "push", ATTACH,
                              "--from", str(index_file))
        assert code == 0
        assert (body["ok"], body["command"]) == (True, "index push")
        assert body["data"] == PUSH_RESULT
        assert fakes.parent_keys == [ATTACH]
        [(parent, index, kwargs)] = fakes.push
        assert parent == PARENT
        assert index == {"schema": "source-index/v1", "facts": []}
        assert kwargs["replace"] is False and kwargs["dry_run"] is False
        assert kwargs["tags"] is None
        assert kwargs["ctx"] is not None
        assert fakes.validate == [index]

    def test_flags_reach_the_engine(self, fakes, monkeypatch, capsys, index_file):
        run_json(monkeypatch, capsys, "index", "push", "KEY1", "--from", str(index_file),
                 "--replace", "--tags", "status/indexed, kind/paper", "--dry-run", "--json")
        [(_parent, _index, kwargs)] = fakes.push
        assert kwargs["replace"] is True and kwargs["dry_run"] is True
        assert kwargs["tags"] == ["status/indexed", "kind/paper"]

    def test_reads_stdin_for_a_dash(self, fakes, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"schema": "source-index/v1"}'))
        code, body = run_json(monkeypatch, capsys, "--json", "index", "push", "KEY1",
                              "--from", "-")
        assert code == 0 and body["ok"] is True
        assert fakes.push[0][1] == {"schema": "source-index/v1"}

    def test_an_invalid_index_is_refused_before_anything_is_written(self, fakes, monkeypatch,
                                                                    capsys, index_file):
        fakes.errors_for_validate.extend(f"problem {n}" for n in range(12))
        code, body = run_json(monkeypatch, capsys, "--json", "index", "push", "KEY1",
                              "--from", str(index_file))
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "invalid_index")
        message = body["error"]["message"]
        assert "12 problem(s)" in message and "problem 0" in message
        assert "problem 11" not in message and "2 more" in message
        assert fakes.push == [] and fakes.parent_keys == []

    def test_unparseable_json_is_invalid_index(self, fakes, monkeypatch, capsys, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, body = run_json(monkeypatch, capsys, "--json", "index", "push", "KEY1",
                              "--from", str(bad))
        assert code == 1
        assert body["error"]["code"] == "invalid_index"
        assert fakes.push == [] and fakes.validate == []

    def test_a_missing_file_is_invalid_index(self, fakes, monkeypatch, capsys, tmp_path):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "push", "KEY1",
                              "--from", str(tmp_path / "absent.json"))
        assert code == 1
        assert body["error"]["code"] == "invalid_index"
        assert "absent.json" in body["error"]["message"]

    def test_an_engine_refusal_keeps_its_code(self, fakes, monkeypatch, capsys, index_file):
        refusal = CliError("An index already exists; pass --replace", code="index_exists")
        with patch.object(sys.modules["zotero_mcp.source_index"], "push_index",
                          side_effect=refusal):
            code, body = run_json(monkeypatch, capsys, "--json", "index", "push", "KEY1",
                                  "--from", str(index_file))
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "index_exists")

    def test_markdown_summary(self, fakes, monkeypatch, capsys, index_file):
        code, out, _err = run_cli(monkeypatch, capsys, "index", "push", "KEY1",
                                  "--from", str(index_file))
        assert code == 0
        assert f"Indexed {PARENT}: 2 note part(s), 210000 characters" in out
        assert "created 2, updated 0, trashed 0" in out
        assert "facts 1200, sections 9" in out
        assert "NOTE0001, NOTE0002" in out

    def test_markdown_summary_of_a_dry_run(self, fakes, monkeypatch, capsys, index_file):
        with patch.object(sys.modules["zotero_mcp.source_index"], "push_index",
                          return_value=dict(PUSH_RESULT, dry_run=True, note_keys=[],
                                            created=["a", "b"])):
            _code, out, _err = run_cli(monkeypatch, capsys, "index", "push", "KEY1",
                                       "--from", str(index_file), "--dry-run")
        assert out.startswith(f"Dry run for {PARENT}")
        assert "created 2" in out


class TestIndexShow:
    def test_shows_the_index_of_the_parent(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH)
        assert code == 0
        assert (body["ok"], body["command"]) == (True, "index show")
        assert body["data"] == SHOW_RESULT
        assert fakes.parent_keys == [ATTACH]
        [(parent, kwargs)] = fakes.show
        assert parent == PARENT
        assert kwargs["section"] is None and kwargs["ctx"] is not None

    def test_section_reaches_the_engine(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "index", "show", "KEY1", "--section", "S03", "--json")
        assert fakes.show[0][1]["section"] == "S03"

    def test_without_pages_the_index_is_not_sliced(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1")
        run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1", "--pages", "all")
        assert fakes.slice == []

    def test_pages_slice_the_index_after_it_is_read(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                              "--pages", "3-5,9")
        assert code == 0
        assert (body["ok"], body["command"]) == (True, "index show")
        assert fakes.slice == [(SHOW_RESULT["index"], [3, 4, 5, 9])]
        data = body["data"]
        assert list(data) == ["item_key", "note_keys", "parts", "index"]
        assert data["index"] == {**SHOW_RESULT["index"], "pages_kept": [3, 4, 5, 9]}
        assert {k: v for k, v in data.items() if k != "index"} == \
            {k: v for k, v in SHOW_RESULT.items() if k != "index"}

    def test_section_and_pages_compose(self, fakes, monkeypatch, capsys):
        """The section is cut first, then the pages: the slice gets the section's index."""
        fakes.index_pages = 1200
        cut = dict(SHOW_RESULT, index={"schema": "source-index/v1", "facts": [], "only": "S03"})
        with patch.object(sys.modules["zotero_mcp.source_index"], "show_index",
                          return_value=cut) as show:
            code, body = run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1",
                                  "--section", "S03", "--pages", "665-666")
        assert code == 0
        assert show.call_args.kwargs["section"] == "S03"
        assert fakes.slice == [(cut["index"], [665, 666])]
        assert body["data"]["index"] == {**cut["index"], "pages_kept": [665, 666]}

    def test_a_page_the_index_lacks_is_bad_pages(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1",
                              "--pages", "41")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "bad_pages")

    def test_an_unparseable_page_list_fails_before_the_index_is_read(self, fakes, monkeypatch,
                                                                      capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1",
                              "--pages", "0")
        assert code == 1
        assert body["error"]["code"] == "bad_pages"
        assert fakes.show == [] and fakes.slice == []

    def test_markdown_shows_the_sliced_index(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "index", "show", "KEY1", "--pages", "3")
        assert code == 0
        assert json.loads(out.split("\n\n", 1)[1]) == {**SHOW_RESULT["index"],
                                                        "pages_kept": [3]}

    def test_no_index_keeps_its_code(self, fakes, monkeypatch, capsys):
        missing = CliError("No source index on this item", code="no_index")
        with patch.object(sys.modules["zotero_mcp.source_index"], "show_index",
                          side_effect=missing):
            code, body = run_json(monkeypatch, capsys, "--json", "index", "show", "KEY1")
        assert code == 1
        assert (body["ok"], body["error"]["code"]) == (False, "no_index")

    def test_markdown_shows_a_header_and_the_index(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "index", "show", "KEY1")
        assert code == 0
        assert out.startswith(f"Source index for {PARENT}: 1 note part(s) (NOTE0001)")
        assert json.loads(out.split("\n\n", 1)[1]) == SHOW_RESULT["index"]


class TestIndexShowLegacy:
    """No --grep, --fields or --limit: today's output, and `index_grep` is never called."""

    def test_json_output_is_byte_identical(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "--json", "index", "show", ATTACH)
        expected = json.dumps({"ok": True, "command": "index show", "schema": 1,
                               "data": SHOW_RESULT}, ensure_ascii=False, default=str) + "\n"
        assert (code, out) == (0, expected)

    def test_json_output_with_pages_is_byte_identical(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                                  "--pages", "3-4")
        data = {**SHOW_RESULT, "index": {**SHOW_RESULT["index"], "pages_kept": [3, 4]}}
        expected = json.dumps({"ok": True, "command": "index show", "schema": 1, "data": data},
                              ensure_ascii=False, default=str) + "\n"
        assert (code, out) == (0, expected)

    def test_markdown_output_is_byte_identical(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "index", "show", ATTACH)
        expected = (f"Source index for {PARENT}: 1 note part(s) (NOTE0001)\n\n"
                    + json.dumps(SHOW_RESULT["index"], indent=2, ensure_ascii=False) + "\n")
        assert (code, out) == (0, expected)

    @pytest.mark.parametrize("flags", [
        (), ("--pages", "3-5"), ("--pages", "all"), ("--section", "S03"),
        ("--section", "S03", "--pages", "3"),
    ])
    def test_index_grep_is_not_called(self, fakes, monkeypatch, capsys, flags):
        code, _body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, *flags)
        assert code == 0
        assert fakes.index_grep == [] and fakes.filters == []

    def test_a_failing_legacy_call_does_not_call_it_either(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--pages", "0")
        assert (code, body["error"]["code"]) == (1, "bad_pages")
        assert fakes.index_grep == []


class TestIndexShowFiltered:
    @pytest.mark.parametrize("flags", [
        ("--grep", "leakage"), ("--fields", "full"), ("--fields", "lead"), ("--limit", "5"),
        ("--limit", "0"),
    ])
    def test_any_of_grep_fields_and_limit_turns_the_filtered_view_on(self, fakes, monkeypatch,
                                                                    capsys, flags):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, *flags)
        assert code == 0
        assert (body["ok"], body["command"]) == (True, "index show")
        assert len(fakes.filters) == 1
        assert "filter" in body["data"]

    def test_the_section_goes_to_show_index_and_the_rest_to_apply_filters(self, fakes, monkeypatch,
                                                                         capsys):
        code, _body = run_json(
            monkeypatch, capsys, "--json", "index", "show", ATTACH, "--section", "S03",
            "--pages", "3-5,9", "--grep", "leakage,seal", "--grep", "gap", "--expand",
            "--fields", "lead", "--limit", "7")
        assert code == 0
        [(parent, show_kwargs)] = fakes.show
        assert parent == PARENT
        assert show_kwargs["section"] == "S03" and show_kwargs["ctx"] is not None
        [(index, filter_kwargs)] = fakes.filters
        assert index == SHOW_RESULT["index"]
        assert filter_kwargs == {"pages": [3, 4, 5, 9], "terms": ["leakage", "seal", "gap"],
                                 "regex": False, "expand": True, "fields": "lead", "limit": 7}

    def test_the_limit_is_40_and_the_fields_are_full_when_omitted(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--grep", "leakage")
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--fields", "lead")
        first, second = (kwargs for _index, kwargs in fakes.filters)
        assert (first["limit"], first["fields"]) == (40, "full")
        assert (second["limit"], second["fields"]) == (40, "lead")
        assert second["terms"] is None and first["pages"] is None

    def test_a_limit_of_zero_reaches_apply_filters_as_zero(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--limit", "0")
        assert fakes.filters[0][1]["limit"] == 0

    @pytest.mark.parametrize("pages, expected", [(None, None), ("all", None), ("665-666", [665, 666])])
    def test_pages_go_to_apply_filters_and_never_to_the_slice(self, fakes, monkeypatch, capsys,
                                                             pages, expected):
        fakes.index_pages = 1200
        flags = ("--pages", pages) if pages else ()
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--limit", "5", *flags)
        assert fakes.filters[0][1]["pages"] == expected
        assert fakes.slice == []

    def test_the_data_keys_end_with_filter(self, fakes, monkeypatch, capsys):
        _code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                               "--grep", "leakage")
        assert list(body["data"]) == ["item_key", "note_keys", "parts", "index", "filter"]

    def test_the_filter_keys_follow_the_contract_order(self, fakes, monkeypatch, capsys):
        _code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                               "--grep", "leakage", "--section", "S03")
        assert list(body["data"]["filter"]) == [
            "terms", "regex", "expand", "fields", "limit", "pages", "section", "expanded_terms",
            "expanded_total", "expanded_symbols", "broad_terms", "term_counts", "total", "matched",
            "returned", "truncated"]

    @pytest.mark.parametrize("flags, section", [(("--section", "S03"), "S03"), ((), None)])
    def test_filter_section_is_the_flag_or_null(self, fakes, monkeypatch, capsys, flags, section):
        _code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                               "--grep", "leakage", *flags)
        assert body["data"]["filter"]["section"] == section

    def test_the_result_carries_the_index_apply_filters_returned(self, fakes, monkeypatch, capsys):
        with patch.object(sys.modules["zotero_mcp.source_index"], "show_index",
                          return_value=indexed(PARENT, facts=60)):
            _code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                                   "--grep", "leakage", "--limit", "10")
        data = body["data"]
        assert [f["id"] for f in data["index"]["facts"]] == [f"F{n:04d}" for n in range(1, 11)]
        assert data["filter"]["matched"]["facts"] == 60
        assert data["filter"]["returned"]["facts"] == 10
        assert data["filter"]["truncated"]["facts"] == 50
        assert (data["item_key"], data["parts"]) == (PARENT, 1)

    # --- terms -------------------------------------------------------------------------------

    def test_the_grep_values_reach_parse_terms_as_given(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                 "--grep", "leakage, seal", "--grep", "Wind-back")
        assert fakes.parse_terms == [(["leakage, seal", "Wind-back"], False)]

    def test_terms_split_on_commas_repeat_and_drop_duplicates(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                 "--grep", "leakage, seal", "--grep", "Leakage", "--grep", "rim,, seal")
        assert fakes.filters[0][1]["terms"] == ["leakage", "seal", "rim"]

    def test_a_regex_keeps_its_comma(self, fakes, monkeypatch, capsys):
        _code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                               "--grep", "a{1,3}", "--regex")
        assert fakes.parse_terms == [(["a{1,3}"], True)]
        assert fakes.check_terms == [(["a{1,3}"], True)]
        assert fakes.filters[0][1]["terms"] == ["a{1,3}"] and fakes.filters[0][1]["regex"] is True
        assert body["data"]["filter"]["regex"] is True

    # --- errors, all before the read -----------------------------------------------------------

    def test_an_empty_term_list_is_bad_grep_before_the_read(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--grep", ",")
        assert (code, body["ok"], body["error"]["code"]) == (1, False, "bad_grep")
        assert fakes.show == [] and fakes.parent_keys == [] and fakes.filters == []

    @pytest.mark.parametrize("flags", [
        ("--expand",), ("--regex",), ("--expand", "--regex"), ("--fields", "lead", "--expand"),
        ("--limit", "5", "--regex"),
    ])
    def test_expand_or_regex_without_grep_is_bad_grep_before_the_read(self, fakes, monkeypatch,
                                                                     capsys, flags):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, *flags)
        assert (code, body["error"]["code"]) == (1, "bad_grep")
        assert fakes.show == [] and fakes.filters == []

    def test_a_pattern_that_does_not_compile_is_bad_regex_before_the_read(self, fakes, monkeypatch,
                                                                         capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                              "--grep", "(", "--regex")
        assert (code, body["error"]["code"]) == (1, "bad_regex")
        assert fakes.show == [] and fakes.parent_keys == [] and fakes.filters == []

    def test_an_unparseable_page_list_is_bad_pages_before_the_read(self, fakes, monkeypatch,
                                                                    capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                              "--pages", "0", "--grep", "leakage")
        assert (code, body["error"]["code"]) == (1, "bad_pages")
        assert fakes.show == [] and fakes.filters == []

    def test_the_pages_are_checked_before_the_terms(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                              "--pages", "0", "--grep", ",")
        assert (code, body["error"]["code"]) == (1, "bad_pages")
        assert fakes.parse_terms == []

    def test_the_terms_are_checked_before_the_notes_are_read(self, fakes, monkeypatch, capsys):
        run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH, "--grep", "leakage",
                 "--regex")
        assert fakes.index_grep[:3] == ["parse_terms", "check_terms", "apply_filters"]
        assert len(fakes.show) == 1

    def test_a_page_past_the_index_is_bad_pages_from_the_filter(self, fakes, monkeypatch, capsys):
        code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                              "--pages", "41", "--limit", "5")
        assert (code, body["error"]["code"]) == (1, "bad_pages")
        assert len(fakes.show) == 1  # the header holds the page count, so the read comes first

    @pytest.mark.parametrize("code_name", ["no_index", "bad_section"])
    def test_the_read_errors_keep_their_codes_when_filtered(self, fakes, monkeypatch, capsys,
                                                          code_name):
        failure = CliError("cannot read", code=code_name)
        with patch.object(sys.modules["zotero_mcp.source_index"], "show_index",
                          side_effect=failure):
            code, body = run_json(monkeypatch, capsys, "--json", "index", "show", ATTACH,
                                  "--grep", "leakage")
        assert (code, body["error"]["code"]) == (1, code_name)
        assert fakes.filters == []

    # --- markdown ----------------------------------------------------------------------------

    def test_markdown_puts_the_filter_line_under_the_header_then_the_index_json(self, fakes,
                                                                                monkeypatch,
                                                                                capsys):
        fakes.expanded, fakes.broad = ["a", "b", "c"], ["seal"]
        with patch.object(sys.modules["zotero_mcp.source_index"], "show_index",
                          return_value=indexed(PARENT, facts=120)):
            code, out, _err = run_cli(monkeypatch, capsys, "index", "show", ATTACH,
                                      "--grep", "leakage", "--limit", "5")
        assert code == 0
        head, body = out.split("\n\n", 1)
        assert head.splitlines() == [
            f"Source index for {PARENT}: 1 note part(s) (NOTE-{PARENT})",
            "Filter: 120 of 120 facts matched, 5 returned, 115 cut by --limit; expanded 3; "
            "broad [seal]"]
        assert len(json.loads(body)["facts"]) == 5

    def test_the_filter_line_counts_facts_and_names_the_broad_terms(self):
        data = {"item_key": PARENT, "note_keys": ["N1"], "parts": 1, "index": {"facts": []},
                "filter": {"total": {"facts": 300}, "matched": {"facts": 12},
                           "returned": {"facts": 10}, "truncated": {"facts": 2},
                           "expanded_total": 5, "broad_terms": ["seal", "gap"]}}
        assert cli_standalone._format_index_show(data).splitlines()[1] == (
            "Filter: 12 of 300 facts matched, 10 returned, 2 cut by --limit; expanded 5; "
            "broad [seal, gap]")


def _stage(fakes, **indexes):
    """Tag the given keys, in the order given, and stage what `show_indexes` returns for each."""
    fakes.tagged = list(indexes)
    fakes.search_indexes = {
        key: value if "error" in value else indexed(key, **value) for key, value in indexes.items()
    }


def _search(monkeypatch, capsys, *argv):
    return run_json(monkeypatch, capsys, "--json", "index", "search", *argv)


class TestIndexSearch:
    def test_the_default_item_set_is_both_status_tags(self, fakes, monkeypatch, capsys):
        _stage(fakes, DDDD0001={"facts": 1}, AAAA0001={"facts": 2})
        code, body = _search(monkeypatch, capsys, "leakage")
        assert (code, body["ok"], body["command"]) == (0, True, "index search")
        assert [tag for tag, _kw in fakes.tag_calls] == \
            ["status/indexed", "status/index-failed-gate"]
        [(keys, ctx)] = fakes.show_indexes
        assert keys == ["DDDD0001", "AAAA0001"] and ctx is not None
        assert fakes.parent_keys == []
        data = body["data"]
        assert (data["tag"], data["items_requested"], data["searched"]) == \
            ("status/indexed,status/index-failed-gate", None, 2)

    def test_a_tag_flag_replaces_the_default(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "leakage", "--tag", "status/checked")
        assert [tag for tag, _kw in fakes.tag_calls] == ["status/checked"]
        assert body["data"]["tag"] == "status/checked"

    def test_a_comma_separated_tag_list_is_the_union_in_first_seen_order(self, fakes, monkeypatch,
                                                                         capsys):
        fakes.search_indexes = {"AAAA0001": indexed("AAAA0001", facts=1),
                                "BBBB0001": indexed("BBBB0001", facts=1),
                                "CCCC0001": indexed("CCCC0001", facts=1)}
        tagged_by_tag = {"status/checked": ["AAAA0001", "BBBB0001"],
                         "status/flagged": ["BBBB0001", "CCCC0001"]}

        def list_items_with_tag(tag, **kwargs):
            fakes.tag_calls.append((tag, kwargs))
            return list(tagged_by_tag.get(tag, []))

        monkeypatch.setattr(sys.modules["zotero_mcp.source_index"], "list_items_with_tag",
                            list_items_with_tag)
        _code, body = _search(monkeypatch, capsys, "leakage", "--tag",
                              "status/checked,status/flagged")
        assert [tag for tag, _kw in fakes.tag_calls] == ["status/checked", "status/flagged"]
        # BBBB0001 comes from both tags; the union keeps it once, where it first appeared.
        assert fakes.show_indexes[0][0] == ["AAAA0001", "BBBB0001", "CCCC0001"]
        assert body["data"]["searched"] == 3
        assert body["data"]["tag"] == "status/checked,status/flagged"

    def test_a_single_tag_with_no_comma_still_works(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "leakage", "--tag", "status/checked")
        assert [tag for tag, _kw in fakes.tag_calls] == ["status/checked"]
        assert body["data"]["searched"] == 1

    def test_blank_parts_of_a_tag_list_are_ignored(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "leakage", "--tag", "status/checked, ,")
        assert [tag for tag, _kw in fakes.tag_calls] == ["status/checked"]
        assert body["data"]["tag"] == "status/checked, ,"

    def test_items_replace_the_tag_and_go_through_parent_key(self, fakes, monkeypatch, capsys):
        fakes.parent_map = {"ATTACH01": "AAAA0001", "BBBB0001": "BBBB0001"}
        fakes.search_indexes = {"AAAA0001": indexed("AAAA0001", facts=1),
                                "BBBB0001": indexed("BBBB0001", facts=1)}
        _code, body = _search(monkeypatch, capsys, "leakage", "--items", "ATTACH01, BBBB0001")
        assert fakes.tag_calls == []
        assert fakes.parent_keys == ["ATTACH01", "BBBB0001"]
        assert fakes.show_indexes[0][0] == ["AAAA0001", "BBBB0001"]
        data = body["data"]
        assert data["tag"] is None
        assert data["items_requested"] == ["ATTACH01", "BBBB0001"]
        assert data["searched"] == 2

    def test_items_that_share_a_parent_are_searched_once(self, fakes, monkeypatch, capsys):
        fakes.parent_map = {"ATTACH01": "AAAA0001", "AAAA0001": "AAAA0001"}
        fakes.search_indexes = {"AAAA0001": indexed("AAAA0001", facts=1)}
        _code, body = _search(monkeypatch, capsys, "leakage", "--items", "ATTACH01,AAAA0001,ATTACH01")
        assert fakes.show_indexes[0][0] == ["AAAA0001"]
        assert len(fakes.filters) == 1
        assert body["data"]["searched"] == 1
        assert [item["item_key"] for item in body["data"]["items"]] == ["AAAA0001"]

    def test_items_ignore_the_tag(self, fakes, monkeypatch, capsys):
        fakes.search_indexes = {"AAAA0001": indexed("AAAA0001", facts=1)}
        _code, body = _search(monkeypatch, capsys, "leakage", "--items", "AAAA0001",
                              "--tag", "status/checked")
        assert fakes.tag_calls == []
        assert body["data"]["tag"] is None

    @pytest.mark.parametrize("flags", [(), ("--items", ","), ("--items", "")])
    def test_an_empty_item_set_is_ok_with_nothing_searched(self, fakes, monkeypatch, capsys, flags):
        code, body = _search(monkeypatch, capsys, "leakage", *flags)
        assert (code, body["ok"]) == (0, True)
        data = body["data"]
        assert (data["searched"], data["items"], data["no_hits"], data["skipped"],
                data["items_truncated"]) == (0, [], [], [], 0)
        assert fakes.show_indexes == [] and fakes.filters == []

    def test_an_empty_items_flag_names_no_tag_and_no_keys(self, fakes, monkeypatch, capsys):
        _code, body = _search(monkeypatch, capsys, "leakage", "--items", ",")
        assert fakes.tag_calls == []
        assert (body["data"]["tag"], body["data"]["items_requested"]) == (None, [])

    # --- terms and flags -------------------------------------------------------------------

    def test_the_terms_reach_parse_terms_and_apply_filters(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "leakage, seal", "Wind-back", "leakage", "--expand")
        assert fakes.parse_terms == [(["leakage, seal", "Wind-back", "leakage"], False)]
        [(index, kwargs)] = fakes.filters
        assert index == fakes.search_indexes["AAAA0001"]["index"]
        assert kwargs == {"pages": None, "terms": ["leakage", "seal", "Wind-back"], "regex": False,
                          "expand": True, "fields": "lead", "limit": 10}
        data = body["data"]
        assert data["terms"] == ["leakage", "seal", "Wind-back"]
        assert (data["regex"], data["expand"], data["fields"], data["limit"], data["max_items"]) == \
            (False, True, "lead", 10, 10)

    def test_the_flags_reach_apply_filters(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "a{1,3}", "--regex", "--fields", "full",
                              "--limit", "0", "--max-items", "3")
        assert fakes.filters[0][1] == {"pages": None, "terms": ["a{1,3}"], "regex": True,
                                       "expand": False, "fields": "full", "limit": 0}
        data = body["data"]
        assert (data["regex"], data["fields"], data["limit"], data["max_items"]) == \
            (True, "full", 0, 3)

    def test_a_regex_keeps_its_comma(self, fakes, monkeypatch, capsys):
        _code, body = _search(monkeypatch, capsys, "a{1,3}", "--regex")
        assert fakes.parse_terms == [(["a{1,3}"], True)]
        assert fakes.check_terms == [(["a{1,3}"], True)]
        assert body["data"]["terms"] == ["a{1,3}"]

    def test_each_item_gets_its_own_index(self, fakes, monkeypatch, capsys):
        _stage(fakes, DDDD0001={"facts": 1}, AAAA0001={"facts": 2})
        _search(monkeypatch, capsys, "leakage")
        assert [index for index, _kw in fakes.filters] == [
            fakes.search_indexes["DDDD0001"]["index"], fakes.search_indexes["AAAA0001"]["index"]]

    # --- errors, all before the read -----------------------------------------------------------

    def test_an_empty_term_list_is_bad_grep_before_any_read(self, fakes, monkeypatch, capsys):
        code, body = _search(monkeypatch, capsys, ",")
        assert (code, body["ok"], body["error"]["code"]) == (1, False, "bad_grep")
        assert fakes.tag_calls == [] and fakes.show_indexes == [] and fakes.parent_keys == []

    def test_a_pattern_that_does_not_compile_is_bad_regex_before_any_read(self, fakes, monkeypatch,
                                                                         capsys):
        code, body = _search(monkeypatch, capsys, "(", "--regex", "--items", "AAAA0001")
        assert (code, body["ok"], body["error"]["code"]) == (1, False, "bad_regex")
        assert fakes.tag_calls == [] and fakes.show_indexes == [] and fakes.parent_keys == []

    # --- the result -------------------------------------------------------------------------

    def test_the_data_keys_follow_the_contract_order(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        _code, body = _search(monkeypatch, capsys, "leakage")
        assert list(body["data"]) == [
            "terms", "regex", "expand", "fields", "limit", "max_items", "tag", "items_requested",
            "searched", "items_truncated", "items", "no_hits", "skipped"]

    def test_an_item_carries_its_header_facts_and_the_filtered_lists(self, fakes, monkeypatch,
                                                                    capsys):
        fakes.tagged = ["AAAA0001"]
        fakes.search_indexes = {"AAAA0001": indexed(
            "AAAA0001", facts=2, vocabulary=1, title="Windback Seals", year="2018",
            attachment="HLCXLQWB", page_count=15)}
        fakes.expanded, fakes.broad = ["wind-back"], ["seal"]
        _code, body = _search(monkeypatch, capsys, "leakage", "--limit", "1")
        [item] = body["data"]["items"]
        assert list(item) == ["item_key", "title", "year", "attachment_key", "page_count", "filter",
                              "sections", "facts", "vocabulary", "tables_figures", "equations",
                              "gaps"]
        assert (item["item_key"], item["title"], item["year"]) == ("AAAA0001", "Windback Seals", "2018")
        assert (item["attachment_key"], item["page_count"]) == ("HLCXLQWB", 15)
        assert list(item["filter"]) == [
            "expanded_terms", "expanded_total", "expanded_symbols", "broad_terms", "term_counts",
            "total", "matched", "returned", "truncated"]
        assert item["filter"]["matched"]["facts"] == 2 and item["filter"]["truncated"]["facts"] == 1
        assert item["filter"]["broad_terms"] == ["seal"]
        assert item["facts"] == make_index(facts=2)["facts"][:1]
        assert item["sections"] == [{"id": "S01", "title": "Intro"}]
        assert len(item["vocabulary"]) == 1

    def test_an_item_with_an_error_is_skipped_and_the_others_are_still_searched(self, fakes,
                                                                                monkeypatch, capsys):
        fakes.tagged = ["AAAA0001", "NOIX0001", "BADX0001", "BOOM0001", "GONE0001"]
        fakes.search_indexes = {
            "AAAA0001": indexed("AAAA0001", facts=2),
            "NOIX0001": {"error": {"code": "no_index", "message": "item NOIX0001 has no source index"}},
            "BADX0001": {"error": {"code": "invalid_index", "message": "part 2 is missing"}},
            "BOOM0001": {"error": {"code": "error", "message": "note fetch failed"}},
            # GONE0001 is absent from what show_indexes returns
        }
        code, body = _search(monkeypatch, capsys, "leakage")
        assert (code, body["ok"]) == (0, True)
        data = body["data"]
        assert [item["item_key"] for item in data["items"]] == ["AAAA0001"]
        assert data["skipped"] == [
            {"item_key": "NOIX0001", "code": "no_index", "message": "item NOIX0001 has no source index"},
            {"item_key": "BADX0001", "code": "invalid_index", "message": "part 2 is missing"},
            {"item_key": "BOOM0001", "code": "error", "message": "note fetch failed"},
            {"item_key": "GONE0001", "code": "error", "message": "no index was read"}]
        assert data["searched"] == 5
        assert len(fakes.filters) == 1  # a skipped item is never filtered

    def test_an_item_with_no_match_in_any_kind_is_listed_in_no_hits(self, fakes, monkeypatch,
                                                                    capsys):
        _stage(fakes, ZZZZ0001={"facts": 0}, HITT0001={"facts": 1}, AAAA0001={"facts": 0})
        _code, body = _search(monkeypatch, capsys, "leakage")
        data = body["data"]
        assert data["no_hits"] == ["AAAA0001", "ZZZZ0001"]
        assert [item["item_key"] for item in data["items"]] == ["HITT0001"]

    def test_an_item_with_only_vocabulary_matches_is_a_hit(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 0, "vocabulary": 2})
        _code, body = _search(monkeypatch, capsys, "leakage")
        assert [item["item_key"] for item in body["data"]["items"]] == ["AAAA0001"]
        assert body["data"]["no_hits"] == []

    def test_items_sort_by_facts_then_all_matches_then_key(self, fakes, monkeypatch, capsys):
        _stage(fakes,
               DDDD0001={"facts": 3},
               EEEE0001={"facts": 0, "vocabulary": 9},
               AAAA0001={"facts": 3},
               CCCC0001={"facts": 3, "vocabulary": 4},
               BBBB0001={"facts": 5})
        _code, body = _search(monkeypatch, capsys, "leakage")
        assert [item["item_key"] for item in body["data"]["items"]] == [
            "BBBB0001", "CCCC0001", "AAAA0001", "DDDD0001", "EEEE0001"]

    def test_max_items_cuts_after_the_sort_and_reports_items_truncated(self, fakes, monkeypatch,
                                                                      capsys):
        _stage(fakes,
               DDDD0001={"facts": 3},
               EEEE0001={"facts": 1},
               AAAA0001={"facts": 3, "vocabulary": 1},
               NONE0001={"facts": 0},
               BBBB0001={"facts": 5})
        fakes.tagged.append("ERRR0001")
        fakes.search_indexes["ERRR0001"] = {"error": {"code": "no_index", "message": "none"}}
        _code, body = _search(monkeypatch, capsys, "leakage", "--max-items", "2")
        data = body["data"]
        assert [item["item_key"] for item in data["items"]] == ["BBBB0001", "AAAA0001"]
        assert (data["max_items"], data["items_truncated"]) == (2, 2)
        assert data["no_hits"] == ["NONE0001"]  # the cut does not touch these two lists
        assert [skip["item_key"] for skip in data["skipped"]] == ["ERRR0001"]
        assert data["searched"] == 6

    def test_max_items_defaults_to_10(self, fakes, monkeypatch, capsys):
        _stage(fakes, **{f"KEY{n:05d}": {"facts": n} for n in range(1, 13)})
        _code, body = _search(monkeypatch, capsys, "leakage")
        data = body["data"]
        assert (len(data["items"]), data["items_truncated"], data["max_items"]) == (10, 2, 10)
        assert data["items"][0]["item_key"] == "KEY00012"

    def test_a_max_items_of_zero_is_no_cap(self, fakes, monkeypatch, capsys):
        _stage(fakes, **{f"KEY{n:05d}": {"facts": n} for n in range(1, 13)})
        _code, body = _search(monkeypatch, capsys, "leakage", "--max-items", "0")
        assert (len(body["data"]["items"]), body["data"]["items_truncated"]) == (12, 0)

    def test_items_truncated_is_zero_when_every_item_fits(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1}, BBBB0001={"facts": 2})
        _code, body = _search(monkeypatch, capsys, "leakage", "--max-items", "2")
        assert body["data"]["items_truncated"] == 0

    # --- markdown ----------------------------------------------------------------------------

    def test_markdown_lists_each_item_with_its_fact_leads(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 2, "vocabulary": 1})
        code, out, _err = run_cli(monkeypatch, capsys, "index", "search", "leakage")
        assert code == 0
        assert out == (
            "## AAAA0001 Windback Seals (2018): 2 facts, 1 vocabulary\n"
            "p1 F0001 leakage rate = 5.34 g/s (at 3 bar) [Table 2]\n"
            "p2 F0002 leakage rate = 5.34 g/s (at 3 bar) [Table 2]\n")

    def test_markdown_leaves_out_a_null_part_of_a_lead(self, fakes, monkeypatch, capsys):
        fakes.tagged = ["AAAA0001"]
        result = indexed("AAAA0001", facts=1)
        result["index"]["facts"][0].update(unit=None, condition=None, ref=None)
        fakes.search_indexes = {"AAAA0001": result}
        _code, out, _err = run_cli(monkeypatch, capsys, "index", "search", "leakage")
        assert out.splitlines()[1] == "p1 F0001 leakage rate = 5.34"

    def test_markdown_names_the_items_without_hits_the_skipped_and_the_cut(self, fakes, monkeypatch,
                                                                          capsys):
        fakes.tagged = ["AAAA0001", "BBBB0001", "NONE0001", "ERRR0001"]
        fakes.search_indexes = {
            "AAAA0001": indexed("AAAA0001", facts=2), "BBBB0001": indexed("BBBB0001", facts=1),
            "NONE0001": indexed("NONE0001", facts=0),
            "ERRR0001": {"error": {"code": "no_index", "message": "no source index"}}}
        _code, out, _err = run_cli(monkeypatch, capsys, "index", "search", "leakage",
                                   "--max-items", "1")
        assert out.startswith("## AAAA0001 Windback Seals (2018): 2 facts, 0 vocabulary\n")
        assert "## BBBB0001" not in out
        assert out.endswith("No hits: NONE0001\n"
                            "Skipped ERRR0001: no_index: no source index\n"
                            "1 more item(s) with hits cut by --max-items\n")

    def test_markdown_says_so_when_there_is_nothing_to_search(self, fakes, monkeypatch, capsys):
        code, out, _err = run_cli(monkeypatch, capsys, "index", "search", "leakage")
        assert (code, out) == (0, "No items to search.\n")


class TestIndexSkipsPymupdf:
    """`main()` reroutes PyMuPDF's messages for every command except `index`."""

    @pytest.fixture
    def rerouted(self, fakes, monkeypatch):
        """Calls to `_keep_pymupdf_off_stdout`; the fakes fixture stubs it, this one records."""
        seen = []
        monkeypatch.setattr(cli_standalone, "_keep_pymupdf_off_stdout", lambda: seen.append("called"))
        return seen

    @pytest.mark.parametrize("argv", [
        ("--json", "index", "show", ATTACH),
        ("--json", "index", "show", ATTACH, "--grep", "leakage"),
        ("--json", "index", "search", "leakage"),
    ])
    def test_index_reads_do_not_call_it(self, rerouted, monkeypatch, capsys, argv):
        code, _out, _err = run_cli(monkeypatch, capsys, *argv)
        assert code == 0
        assert rerouted == []

    def test_index_push_does_not_call_it(self, rerouted, monkeypatch, capsys, index_file):
        code, _out, _err = run_cli(monkeypatch, capsys, "--json", "index", "push", ATTACH,
                                   "--from", str(index_file))
        assert code == 0
        assert rerouted == []

    def test_a_failing_index_call_does_not_call_it(self, rerouted, monkeypatch, capsys):
        code, _out, _err = run_cli(monkeypatch, capsys, "--json", "index", "search", ",")
        assert code == 1
        assert rerouted == []

    def test_grep_still_calls_it(self, rerouted, monkeypatch, capsys):
        code, _out, _err = run_cli(monkeypatch, capsys, "--json", "grep", ATTACH, "pressure ratio")
        assert code == 0
        assert rerouted == ["called"]

    def test_another_command_still_calls_it(self, rerouted, monkeypatch, capsys):
        monkeypatch.setitem(cli_standalone._CMD_MAP, "config", lambda args: None)
        run_cli(monkeypatch, capsys, "--json", "config")
        assert rerouted == ["called"]


# ---------------------------------------------------------------------------
# pdf_source
# ---------------------------------------------------------------------------

class _FakeReader:
    """Just enough of LocalZoteroReader for the lookup order."""

    attachments = {}
    items = {}
    files = {}

    def __init__(self, db_path=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_attachment_by_key(self, key):
        return self.attachments.get(key)

    def resolve_attachment_file(self, key):
        return self.files.get(key)

    def get_item_by_key(self, key):
        return self.items.get(key)

    def _iter_parent_attachments(self, item_id):
        return [(k, None, a["content_type"]) for k, a in self.attachments.items()
                if a["parent_item_id"] == item_id]


@pytest.fixture
def local_library(monkeypatch):
    """A local library: item PARENT01 with a PDF ATTACH01 and a snapshot SNAP0001."""
    _FakeReader.attachments = {
        ATTACH: {"content_type": "application/pdf", "title": "Full Text PDF",
                 "parent_key": PARENT, "parent_item_id": 7},
        "SNAP0001": {"content_type": "text/html", "title": "Snapshot",
                     "parent_key": PARENT, "parent_item_id": 7},
        "LOOSE001": {"content_type": "application/pdf", "title": "Loose scan",
                     "parent_key": None, "parent_item_id": None},
    }
    _FakeReader.items = {PARENT: SimpleNamespace(item_id=7, title=TITLE)}
    _FakeReader.files = {ATTACH: Path(PDF_PATH), "LOOSE001": Path("/library/storage/loose.pdf")}
    monkeypatch.setattr("zotero_mcp.local_db.LocalZoteroReader", _FakeReader)
    monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)
    monkeypatch.setattr("zotero_mcp.config.load_config",
                        lambda: MagicMock(resolve_zotero_db_path=lambda: "zotero.sqlite"))
    return _FakeReader


class TestResolvedPdfLocal:
    def test_an_item_key_finds_its_pdf(self, local_library):
        with pdf_source.resolved_pdf(PARENT, MagicMock()) as pdf:
            assert pdf == pdf_source.ResolvedPdf(PARENT, ATTACH, PDF_PATH, TITLE, False)

    def test_an_attachment_key_finds_that_file_and_its_parent(self, local_library):
        with pdf_source.resolved_pdf(ATTACH, MagicMock()) as pdf:
            assert (pdf.parent_key, pdf.attachment_key) == (PARENT, ATTACH)
            assert pdf.path == PDF_PATH
            # The parent's title, not the attachment's "Full Text PDF".
            assert pdf.title == TITLE
            assert pdf.is_temp is False

    def test_a_standalone_attachment_is_its_own_parent(self, local_library):
        with pdf_source.resolved_pdf("LOOSE001", MagicMock()) as pdf:
            assert (pdf.parent_key, pdf.attachment_key) == ("LOOSE001", "LOOSE001")
            assert pdf.title == "Loose scan"

    def test_a_library_file_is_never_removed(self, local_library):
        with patch("zotero_mcp.tools.read_pdf._cleanup_path") as cleanup:
            with pdf_source.resolved_pdf(PARENT, MagicMock()):
                pass
        cleanup.assert_not_called()

    def test_a_non_pdf_attachment_falls_through_to_the_download(self, local_library,
                                                                monkeypatch):
        monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: True)
        monkeypatch.setattr(pdf_source, "_download", lambda key: None)
        with pytest.raises(CliError) as exc:
            with pdf_source.resolved_pdf("SNAP0001", MagicMock()):
                pass
        assert exc.value.code == "no_pdf_attachment"

    def test_an_empty_key_is_refused(self):
        with pytest.raises(CliError) as exc:
            with pdf_source.resolved_pdf("  ", MagicMock()):
                pass
        assert exc.value.code == "empty_item_key"


def _backend(items):
    backend = MagicMock()
    backend.get_item.side_effect = lambda key: items[key]
    return backend


ITEMS = {
    PARENT: {"key": PARENT, "data": {"key": PARENT, "itemType": "journalArticle",
                                     "title": TITLE}},
    ATTACH: {"key": ATTACH, "data": {"key": ATTACH, "itemType": "attachment",
                                     "title": "Full Text PDF", "parentItem": PARENT,
                                     "contentType": "application/pdf"}},
    "LOOSE001": {"key": "LOOSE001", "data": {"key": "LOOSE001", "itemType": "attachment",
                                             "title": "Loose scan",
                                             "contentType": "application/pdf"}},
}


class TestParentKey:
    @pytest.mark.parametrize("key, expected", [
        (PARENT, PARENT),
        (ATTACH, PARENT),
        ("LOOSE001", "LOOSE001"),
    ])
    def test_maps_a_key_to_its_regular_item(self, monkeypatch, key, expected):
        monkeypatch.setattr("zotero_mcp.library.get_library_backend", lambda: _backend(ITEMS))
        assert pdf_source.parent_key(key) == expected

    def test_an_empty_key_is_refused(self):
        with pytest.raises(CliError):
            pdf_source.parent_key("")


class TestResolvedPdfDownload:
    """Not in local storage: the file comes down into a directory this module owns."""

    @pytest.fixture
    def cloud(self, monkeypatch):
        monkeypatch.setattr("zotero_mcp.utils.is_local_mode", lambda: False)
        monkeypatch.setattr("zotero_mcp.library.get_library_backend", lambda: _backend(ITEMS))
        monkeypatch.setattr("zotero_mcp.client.get_zotero_client", lambda: MagicMock())
        monkeypatch.setattr("zotero_mcp.client.get_local_zotero_client", lambda: None)
        state = SimpleNamespace(dirs=[], attachment=SimpleNamespace(
            key=ATTACH, title="Full Text PDF", filename="paper.pdf",
            content_type="application/pdf"))

        def download(key, dest, filename, **_kwargs):
            path = Path(dest) / filename
            path.write_bytes(b"%PDF-1.4 stub")
            state.dirs.append(Path(dest))
            return SimpleNamespace(path=path)

        monkeypatch.setattr("zotero_mcp.client.get_attachment_details",
                            lambda zot, item, priority=None: state.attachment)
        monkeypatch.setattr("zotero_mcp.client.download_attachment_file", download)
        return state

    def test_an_item_key_downloads_and_cleans_up(self, cloud):
        with pdf_source.resolved_pdf(PARENT, MagicMock()) as pdf:
            assert (pdf.parent_key, pdf.attachment_key, pdf.title) == (PARENT, ATTACH, TITLE)
            assert pdf.is_temp is True
            assert Path(pdf.path).read_bytes().startswith(b"%PDF")
            directory = Path(pdf.path).parent
        assert not directory.exists()

    def test_an_attachment_key_reports_its_parent(self, cloud):
        with pdf_source.resolved_pdf(ATTACH, MagicMock()) as pdf:
            assert (pdf.parent_key, pdf.attachment_key) == (PARENT, ATTACH)
            assert pdf.title == TITLE

    def test_the_copy_is_removed_when_the_body_raises(self, cloud):
        with pytest.raises(RuntimeError):
            with pdf_source.resolved_pdf(PARENT, MagicMock()):
                raise RuntimeError("engine blew up")
        assert cloud.dirs and not cloud.dirs[0].exists()

    def test_no_attachment_reports_no_pdf(self, cloud):
        cloud.attachment = None
        with pytest.raises(CliError) as exc:
            with pdf_source.resolved_pdf(PARENT, MagicMock()):
                pass
        assert exc.value.code == "no_pdf_attachment"
        assert cloud.dirs == []

    def test_an_unknown_key_reports_no_pdf(self, cloud, monkeypatch):
        monkeypatch.setattr("zotero_mcp.library.get_library_backend",
                            lambda: _backend({"NOPE0000": None}))
        with pytest.raises(CliError) as exc:
            with pdf_source.resolved_pdf("NOPE0000", MagicMock()):
                pass
        assert exc.value.code == "no_pdf_attachment"
        assert cloud.dirs == []

    def test_a_non_pdf_attachment_reports_no_pdf(self, cloud):
        cloud.attachment = SimpleNamespace(key="SNAP0001", title="Snapshot",
                                           filename="page.html", content_type="text/html")
        with pytest.raises(CliError) as exc:
            with pdf_source.resolved_pdf(PARENT, MagicMock()):
                pass
        assert exc.value.code == "no_pdf_attachment"
        assert cloud.dirs == []


# ---------------------------------------------------------------------------
# Nothing but the envelope on stdout
# ---------------------------------------------------------------------------

#: Runs `zotero-cli` in a fresh interpreter with the PDF lookups replaced, so a
#: real `import fitz` runs and prints whatever it prints. In process, `fitz` is
#: already imported by the time a test runs and the warning is long gone.
_DRIVER = """
import sys
from unittest.mock import patch

pdf, argv = sys.argv[1], sys.argv[2:]
from zotero_mcp import cli_standalone
from zotero_mcp.tools import annotations, read_pdf

with patch.object(cli_standalone, "setup_zotero_environment"), \\
     patch.object(read_pdf, "_get_pdf_path", return_value=(pdf, "Synthetic", False)), \\
     patch.object(annotations, "_fetch_attachment_file",
                  return_value=(pdf, "synthetic.pdf", "pdf", None)):
    sys.argv = ["zotero-cli", *argv]
    cli_standalone.main()
"""


@pytest.fixture(scope="module")
def synthetic_pdf(tmp_path_factory):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for text in ("Compressor pressure ratio is 14.5 at design.",
                 "The swirler passes 3.2 percent of the air."):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 100), text, fontsize=11)
    path = str(tmp_path_factory.mktemp("pdf") / "synthetic.pdf")
    doc.save(path)
    doc.close()
    return path


def _run_driver(pdf, *argv):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]))
    return subprocess.run([sys.executable, "-c", _DRIVER, pdf, *argv],
                          capture_output=True, text=True, env=env, timeout=120)


class TestJsonOutputIsClean:
    @pytest.mark.parametrize("argv", [
        ("--json", "read", "KEY00001", "--start-page", "1", "--end-page", "2"),
        ("--json", "layout", "ATTACH01", "--pages", "1"),
    ])
    def test_stdout_starts_with_the_envelope(self, synthetic_pdf, argv):
        proc = _run_driver(synthetic_pdf, *argv)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout[:1] == "{", f"stdout began with {proc.stdout[:80]!r}"
        body = json.loads(proc.stdout)
        assert body["ok"] is True
        assert body["command"] == argv[1]

    def test_the_read_text_is_in_the_envelope(self, synthetic_pdf):
        proc = _run_driver(synthetic_pdf, "--json", "read", "KEY00001", "--start-page", "1")
        assert "14.5" in json.loads(proc.stdout)["data"]["text"]

    def test_main_reroutes_pymupdf_messages_before_the_command_runs(self, monkeypatch):
        pymupdf = pytest.importorskip("pymupdf")
        order = []
        monkeypatch.setattr(pymupdf, "set_messages", lambda **kw: order.append(("set", kw)))
        monkeypatch.setitem(cli_standalone._CMD_MAP, "config",
                            lambda args: order.append(("run", None)))
        monkeypatch.setattr(sys, "argv", ["zotero-cli", "config"])
        cli_standalone.main()
        assert order == [("set", {"fd": 2}), ("run", None)]

    def test_a_pymupdf_without_set_messages_is_left_alone(self, monkeypatch):
        pymupdf = pytest.importorskip("pymupdf")
        monkeypatch.delattr(pymupdf, "set_messages")
        cli_standalone._keep_pymupdf_off_stdout()


#: Runs `main()` in a fresh interpreter with the index engines faked, then reports on stderr whether
#: PyMuPDF got loaded. In process the module is long imported, so only a subprocess can tell.
_NO_PYMUPDF_DRIVER = """
import sys, types
from unittest.mock import patch

import zotero_mcp
from zotero_mcp import cli_standalone, pdf_source

engine = types.ModuleType("zotero_mcp.source_index")
engine.show_index = lambda key, **kw: {"item_key": key, "note_keys": [], "parts": 1,
                                       "index": {"facts": []}}
sys.modules["zotero_mcp.source_index"] = engine
zotero_mcp.source_index = engine
argv = ["zotero-cli", *sys.argv[1:]]
with patch.object(cli_standalone, "setup_zotero_environment"), \\
        patch.object(pdf_source, "parent_key", lambda key: key), \\
        patch.dict(cli_standalone._CMD_MAP, {"config": lambda args: None}), \\
        patch.object(sys, "argv", argv):
    cli_standalone.main()
print("PYMUPDF-LOADED", "pymupdf" in sys.modules or "fitz" in sys.modules, file=sys.stderr)
"""


def _run_no_pymupdf_driver(*argv):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]))
    return subprocess.run([sys.executable, "-c", _NO_PYMUPDF_DRIVER, *argv],
                          capture_output=True, text=True, env=env, timeout=120)


class TestIndexPathLoadsNoPymupdf:
    def test_index_show_runs_without_importing_pymupdf(self):
        proc = _run_no_pymupdf_driver("--json", "index", "show", "KEY00001")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["ok"] is True
        assert "PYMUPDF-LOADED False" in proc.stderr

    def test_the_probe_sees_pymupdf_when_another_command_runs(self):
        """The control: without it, a probe that never fires would pass the test above."""
        pytest.importorskip("pymupdf")
        proc = _run_no_pymupdf_driver("--json", "config")
        assert proc.returncode == 0, proc.stderr
        assert "PYMUPDF-LOADED True" in proc.stderr
