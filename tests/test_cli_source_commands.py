"""`zotero-cli grep`, `sections`, `tables` and `index`, and the PDF key resolver under them.

The engines behind these commands (`pdf_grep`, `pdf_sections`, `pdf_tables`,
`source_index`, `index_slice`) are separate modules. These tests replace them with fakes injected through
`sys.modules`, so what is pinned here is the wiring alone: which arguments the
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
                  pdf_pages=1200, index_pages=40)

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

    index_mod.validate_index = validate_index
    index_mod.push_index = push_index
    index_mod.show_index = show_index

    for name, module in (("pdf_grep", grep_mod), ("pdf_sections", sections_mod),
                         ("pdf_tables", tables_mod), ("index_slice", slice_mod),
                         ("source_index", index_mod)):
        _install(monkeypatch, name, module)

    @contextmanager
    def resolved_pdf(key, ctx):
        calls.resolved.append(key)
        yield pdf_source.ResolvedPdf(PARENT, ATTACH, PDF_PATH, TITLE, False)

    def parent_key(key):
        calls.parent_keys.append(key)
        return PARENT

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
