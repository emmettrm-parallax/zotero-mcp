"""`zotero_index_show` and `zotero_index_search`: the MCP path to the source index.

Fakes replace `source_index` and `index_grep` through `sys.modules`, the same technique
`test_cli_source_commands.py` uses for the CLI path, so these tests pin the MCP tools'
own wiring on top of the shared `index_query` functions: the bad_grep pre-check that
needs no backend call, the hard limit cap, and that the same inputs give the same `data`
through the tool function and through `zotero-cli --json index show|search`.
"""

import json
import sys
import types
from types import SimpleNamespace

import pytest
from conftest import DummyContext

from zotero_mcp import cli_standalone, pdf_source
from zotero_mcp.cli_json import CliError
from zotero_mcp.cli_standalone import build_parser
from zotero_mcp.tools import index_tools
from zotero_mcp.toolsets import DEFAULT_ON, apply_toolsets, resolve_enabled

PARENT = "PARENT01"
ATTACH = "ATTACH01"

INDEX_KINDS = ("facts", "vocabulary", "tables_figures", "equations", "gaps")


def fact_record(number):
    return {"id": f"F{number:04d}", "page": number, "kind": "text", "quantity": "leakage rate",
            "value": "5.34", "unit": "g/s", "condition": "at 3 bar", "ref": "Table 2",
            "section_id": "S01"}


def make_index(facts=0, *, title="Windback Seals", year="2018", attachment=ATTACH,
               page_count=15):
    return {
        "schema": "source-index/v1",
        "header": {"attachment_key": attachment, "title": title, "year": year,
                   "page_count": page_count},
        "sections": [{"id": "S01", "title": "Intro"}],
        "vocabulary": [],
        "facts": [fact_record(n) for n in range(1, facts + 1)],
        "tables_figures": [], "equations": [], "gaps": [],
    }


def indexed(key, **kwargs):
    return {"item_key": key, "note_keys": [f"NOTE-{key}"], "parts": 1, "index": make_index(**kwargs)}


SHOW_RESULT = indexed(PARENT, facts=60)


def _install(monkeypatch, name, module):
    """Make `from zotero_mcp import <name>` find *module* (see test_cli_source_commands.py)."""
    import zotero_mcp

    monkeypatch.setitem(sys.modules, f"zotero_mcp.{name}", module)
    monkeypatch.setattr(zotero_mcp, name, module, raising=False)


@pytest.fixture
def fakes(monkeypatch):
    calls = SimpleNamespace(show=[], filters=[], parse_terms=[], check_terms=[],
                           show_indexes=[], tag_calls=[], tagged=[], search_indexes={},
                           parent_map={}, parent_keys=[])

    index_mod = types.ModuleType("zotero_mcp.source_index")

    def show_index(parent_key, **kwargs):
        calls.show.append((parent_key, kwargs))
        return dict(SHOW_RESULT)

    def show_indexes(parent_keys, *, ctx):
        calls.show_indexes.append((list(parent_keys), ctx))
        return {key: calls.search_indexes[key] for key in parent_keys if key in calls.search_indexes}

    def list_items_with_tag(tag, **kwargs):
        calls.tag_calls.append((tag, kwargs))
        return list(calls.tagged)

    index_mod.show_index = show_index
    index_mod.show_indexes = show_indexes
    index_mod.list_items_with_tag = list_items_with_tag

    grep_mod = types.ModuleType("zotero_mcp.index_grep")

    def parse_terms(values, *, regex):
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
        calls.check_terms.append((list(terms), regex))
        import re
        for term in terms if regex else ():
            try:
                re.compile(term)
            except re.error as exc:
                raise CliError(f"Bad regex {term!r}: {exc}", code="bad_regex") from exc

    def apply_filters(index, *, pages=None, terms=None, regex=False, expand=False, fields="full",
                      limit=40):
        calls.filters.append((index, dict(pages=pages, terms=terms, regex=regex, expand=expand,
                                          fields=fields, limit=limit)))
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
            "expanded_terms": [], "expanded_total": 0, "expanded_symbols": [], "broad_terms": [],
            "term_counts": {term: 1 for term in terms or []},
            "total": total, "matched": dict(total), "returned": returned,
            "truncated": {kind: total[kind] - returned[kind] for kind in INDEX_KINDS},
        }

    grep_mod.parse_terms = parse_terms
    grep_mod.check_terms = check_terms
    grep_mod.apply_filters = apply_filters

    for name, module in (("source_index", index_mod), ("index_grep", grep_mod)):
        _install(monkeypatch, name, module)

    def parent_key(key):
        calls.parent_keys.append(key)
        return calls.parent_map.get(key, PARENT)

    monkeypatch.setattr(pdf_source, "parent_key", parent_key)
    monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
    return calls


def run_cli_json(monkeypatch, capsys, *argv):
    """Run `zotero-cli --json *argv` in process. Return the exit code and the parsed body."""
    monkeypatch.setattr(sys, "argv", ["zotero-cli", "--json", *argv])
    code = 0
    try:
        cli_standalone.main()
    except SystemExit as exc:
        code = exc.code or 0
    out = capsys.readouterr().out
    return code, json.loads(out)


def _stage(fakes, **indexes):
    fakes.tagged = list(indexes)
    fakes.search_indexes = {key: indexed(key, **value) for key, value in indexes.items()}
    fakes.parent_map.update({key: key for key in indexes})


# ---------------------------------------------------------------------------
# Parity: the MCP tool and the CLI give the same `data` for the same inputs
# ---------------------------------------------------------------------------

class TestShowParity:
    def test_defaults_are_fields_lead_and_limit_40(self, fakes):
        index_tools.index_show(ATTACH, grep=["leakage"], ctx=DummyContext())
        assert fakes.filters[0][1]["fields"] == "lead"
        assert fakes.filters[0][1]["limit"] == 40

    def test_same_data_as_the_cli(self, fakes, monkeypatch, capsys):
        tool_out = index_tools.index_show(ATTACH, grep=["leakage"], ctx=DummyContext())

        fakes.filters.clear()
        _code, body = run_cli_json(monkeypatch, capsys, "index", "show", ATTACH,
                                   "--grep", "leakage", "--fields", "lead", "--limit", "40")
        assert tool_out["data"] == body["data"]
        assert tool_out["ok"] is True and tool_out["command"] == "index show"


class TestSearchParity:
    def test_defaults_are_fields_lead_and_limit_10(self, fakes):
        _stage(fakes, AAAA0001={"facts": 1})
        index_tools.index_search(["leakage"], items="AAAA0001", ctx=DummyContext())
        assert fakes.filters[0][1]["fields"] == "lead"
        assert fakes.filters[0][1]["limit"] == 10

    def test_same_data_as_the_cli(self, fakes, monkeypatch, capsys):
        _stage(fakes, AAAA0001={"facts": 1})
        tool_out = index_tools.index_search(["leakage"], items="AAAA0001", ctx=DummyContext())

        fakes.filters.clear()
        _code, body = run_cli_json(monkeypatch, capsys, "index", "search", "leakage",
                                   "--items", "AAAA0001", "--fields", "lead", "--limit", "10",
                                   "--max-items", "10")
        assert tool_out["data"] == body["data"]
        assert tool_out["ok"] is True and tool_out["command"] == "index search"


# ---------------------------------------------------------------------------
# The bare-read guard
# ---------------------------------------------------------------------------

class TestBadGrepGuard:
    def test_only_item_key_is_bad_grep_with_no_backend_call(self, fakes):
        out = index_tools.index_show(ATTACH, ctx=DummyContext())
        assert out == {"ok": False, "command": "index show",
                       "error": out["error"], "schema": out["schema"]}
        assert out["error"]["code"] == "bad_grep"
        assert fakes.show == [] and fakes.parent_keys == []

    def test_only_section_is_bad_grep_with_no_backend_call(self, fakes):
        out = index_tools.index_show(ATTACH, section="S03", ctx=DummyContext())
        assert out["ok"] is False and out["error"]["code"] == "bad_grep"
        assert fakes.show == [] and fakes.parent_keys == []

    def test_grep_alone_is_allowed(self, fakes):
        out = index_tools.index_show(ATTACH, grep=["leakage"], ctx=DummyContext())
        assert out["ok"] is True
        assert len(fakes.show) == 1

    def test_pages_alone_is_allowed(self, fakes):
        out = index_tools.index_show(ATTACH, pages="3-5", ctx=DummyContext())
        assert out["ok"] is True
        assert len(fakes.show) == 1


# ---------------------------------------------------------------------------
# The limit cap
# ---------------------------------------------------------------------------

class TestLimitCap:
    @pytest.mark.parametrize("requested", [200, 0])
    def test_show_limit_clamps_to_80(self, fakes, requested):
        out = index_tools.index_show(ATTACH, grep=["leakage"], limit=requested, ctx=DummyContext())
        assert fakes.filters[0][1]["limit"] == 80
        assert out["data"]["filter"]["limit_clamped_from"] == requested

    @pytest.mark.parametrize("requested", [200, 0])
    def test_search_limit_clamps_to_80(self, fakes, requested):
        _stage(fakes, AAAA0001={"facts": 1})
        out = index_tools.index_search(["leakage"], items="AAAA0001", limit=requested,
                                       ctx=DummyContext())
        assert fakes.filters[0][1]["limit"] == 80
        assert out["data"]["limit_clamped_from"] == requested

    def test_an_unclamped_limit_carries_no_marker(self, fakes):
        out = index_tools.index_show(ATTACH, grep=["leakage"], limit=15, ctx=DummyContext())
        assert "limit_clamped_from" not in out["data"]["filter"]


# ---------------------------------------------------------------------------
# A CliError becomes a failure envelope, never a raised exception
# ---------------------------------------------------------------------------

class TestErrorsBecomeEnvelopes:
    def test_bad_regex_is_an_envelope(self, fakes):
        out = index_tools.index_show(ATTACH, grep=["("], regex=True, ctx=DummyContext())
        assert out == {"ok": False, "command": "index show",
                       "error": {"message": out["error"]["message"], "code": "bad_regex"},
                       "schema": out["schema"]}

    def test_no_index_is_an_envelope(self, fakes, monkeypatch):
        def failing_show_index(parent_key, **kwargs):
            raise CliError("No source index on this item", code="no_index")

        monkeypatch.setattr(sys.modules["zotero_mcp.source_index"], "show_index",
                           failing_show_index)
        out = index_tools.index_show(ATTACH, grep=["leakage"], ctx=DummyContext())
        assert (out["ok"], out["error"]["code"]) == (False, "no_index")

    def test_search_bad_grep_is_an_envelope(self, fakes):
        out = index_tools.index_search([","], ctx=DummyContext())
        assert (out["ok"], out["error"]["code"]) == (False, "bad_grep")
        assert fakes.show_indexes == [] and fakes.tag_calls == []


# ---------------------------------------------------------------------------
# Toolset registration
# ---------------------------------------------------------------------------

class TestToolsetRegistration:
    def test_resolve_enabled_holds_the_group(self):
        assert "source-index" in resolve_enabled()
        assert "source-index" in DEFAULT_ON

    def test_none_hides_both_tools(self):
        from zotero_mcp.server import mcp

        try:
            enabled = apply_toolsets(mcp, raw="none")
            assert "source-index" not in enabled
            assert "source-index" not in resolve_enabled("none")
        finally:
            apply_toolsets(mcp, raw="all", transport="streamable-http")

    def test_default_shows_both_tools(self):
        assert resolve_enabled() >= {"source-index"}


# ---------------------------------------------------------------------------
# The CLI parser still works with the module moved out from under it
# ---------------------------------------------------------------------------

def test_cli_index_show_parser_still_parses():
    args = build_parser().parse_args(["index", "show", "KEY1", "--grep", "leakage"])
    assert args.command == "index" and args.subcommand == "show"
