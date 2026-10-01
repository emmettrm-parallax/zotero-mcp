"""`source_card` (the engine) and `zotero-cli index cards` / `index cards push` (the CLI).

`validate_card`, `render_card_note` and `parse_card_note` are pure, so most tests build a
card dict and check the function's return value directly. `push_card` and `list_cards`
reach the library through fake `zotero_mcp.library` and `zotero_mcp.tools.annotations`
modules, injected through `sys.modules` the way `test_cli_source_commands.py` fakes
`source_index`. The CLI tests fake the whole `zotero_mcp.source_card` module instead, so
what they pin is the wiring: which arguments `cmd_index` hands the engine, how `--expand`
changes the JSON shape, and the error codes a bad card or a bad key produce.

The last test guards the import: a card is text, never a PDF page, so the module must not
load pymupdf.
"""

import importlib
import io
import json
import os
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

from zotero_mcp import cli_standalone, pdf_source, source_card
from zotero_mcp.cli_json import CliError
from zotero_mcp.cli_standalone import build_parser

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
SRC = REPO / "src"

VALID_CARD = {
    "schema": "source-card/v1",
    "item_key": "ABCD1234",
    "title_short": "Windback seal leakage under axial clearance",
    "kind": "paper",
    "topics": ["windback seal", "leakage rate", "axial clearance", "oil containment", "bearing sump"],
    "synonyms": ["windback", "wind back seal", "screw seal", "return thread seal", "oil slinger",
                 "labyrinth alternative", "axial groove seal", "rotor seal", "bearing seal", "thread seal"],
    "quantities": ["leakage rate", "clearance"],
    "scope": "What leakage rate a windback seal holds at a given axial clearance and speed.",
    "not_about": "Labyrinth seal design or brush seal wear.",
    "pages": 12,
    "tags": [],
}


def _card(**overrides) -> dict:
    return {**VALID_CARD, **overrides}


def _install(monkeypatch, dotted: str, module) -> None:
    """Make `from <package> import <leaf>` find *module*.

    Both places are set, as `test_cli_source_commands.py` does for `source_index`: a real
    module an earlier test already imported sits as an attribute on the package, and
    `from package import name` reads that attribute before it looks in `sys.modules`.
    """
    package_name, _, leaf = dotted.rpartition(".")
    package = importlib.import_module(package_name)
    monkeypatch.setitem(sys.modules, dotted, module)
    monkeypatch.setattr(package, leaf, module, raising=False)


# ---------------------------------------------------------------------------
# validate_card
# ---------------------------------------------------------------------------

class TestValidateCard:
    def test_valid_card_has_no_problems(self):
        assert source_card.validate_card(VALID_CARD) == []

    def test_not_a_dict(self):
        assert source_card.validate_card([1, 2]) == ["card must be a JSON object"]

    def test_unknown_key_is_a_problem(self):
        problems = source_card.validate_card(_card(extra="nope"))
        assert any("unknown key" in p and "extra" in p for p in problems)

    def test_missing_key_is_a_problem(self):
        card = dict(VALID_CARD)
        del card["pages"]
        problems = source_card.validate_card(card)
        assert any("missing top-level key" in p and "pages" in p for p in problems)

    def test_wrong_schema_is_a_problem(self):
        assert source_card.validate_card(_card(schema="source-index/v1"))

    @pytest.mark.parametrize("key", ["ABCD123", "abcd1234", "ABCD-234", "ABCD12345", 1234])
    def test_bad_item_key(self, key):
        assert source_card.validate_card(_card(item_key=key))

    def test_good_item_key(self):
        assert source_card.validate_card(_card(item_key="A1B2C3D4")) == []

    @pytest.mark.parametrize("kind", ["paper", "book", "report", "web"])
    def test_good_kind(self, kind):
        assert source_card.validate_card(_card(kind=kind)) == []

    def test_bad_kind(self):
        assert source_card.validate_card(_card(kind="blog"))

    def test_title_short_at_the_limit(self):
        assert source_card.validate_card(_card(title_short="x" * 60)) == []

    def test_title_short_over_the_limit(self):
        assert source_card.validate_card(_card(title_short="x" * 61))

    def test_title_short_empty(self):
        assert source_card.validate_card(_card(title_short=""))

    @pytest.mark.parametrize("key", ["scope", "not_about"])
    def test_sentence_at_the_limit(self, key):
        assert source_card.validate_card(_card(**{key: "x" * 200})) == []

    @pytest.mark.parametrize("key", ["scope", "not_about"])
    def test_sentence_over_the_limit(self, key):
        assert source_card.validate_card(_card(**{key: "x" * 201}))

    @pytest.mark.parametrize("n,ok", [(4, False), (5, True), (8, True), (9, False)])
    def test_topics_count(self, n, ok):
        problems = source_card.validate_card(_card(topics=[f"topic {i}" for i in range(n)]))
        assert (problems == []) == ok

    @pytest.mark.parametrize("n,ok", [(9, False), (10, True), (20, True), (21, False)])
    def test_synonyms_count(self, n, ok):
        problems = source_card.validate_card(_card(synonyms=[f"synonym {i}" for i in range(n)]))
        assert (problems == []) == ok

    @pytest.mark.parametrize("n,ok", [(0, True), (10, True), (11, False)])
    def test_quantities_count(self, n, ok):
        problems = source_card.validate_card(_card(quantities=[f"q{i}" for i in range(n)]))
        assert (problems == []) == ok

    def test_tags_has_no_stated_floor_or_ceiling(self):
        assert source_card.validate_card(_card(tags=[])) == []
        assert source_card.validate_card(_card(tags=[f"t{i}" for i in range(40)])) == []

    def test_list_item_over_60_chars_is_a_problem(self):
        assert source_card.validate_card(_card(tags=["x" * 61]))

    def test_list_item_empty_string_is_a_problem(self):
        assert source_card.validate_card(_card(tags=[""]))

    def test_list_item_not_a_string_is_a_problem(self):
        assert source_card.validate_card(_card(tags=[1]))

    @pytest.mark.parametrize("pages", [1, 500, None])
    def test_good_pages(self, pages):
        assert source_card.validate_card(_card(pages=pages)) == []

    @pytest.mark.parametrize("pages", [0, -1, 1.5, "12", True])
    def test_bad_pages(self, pages):
        assert source_card.validate_card(_card(pages=pages))


# ---------------------------------------------------------------------------
# render_card_note / parse_card_note
# ---------------------------------------------------------------------------

class TestRenderAndParse:
    def test_round_trip(self):
        note_html = source_card.render_card_note(VALID_CARD)
        assert note_html.startswith("<h1>Source card</h1><p>")
        assert "<pre>source-card/v1\n" in note_html
        assert source_card.parse_card_note(note_html) == VALID_CARD

    def test_the_data_block_is_ascii(self):
        card = _card(title_short="Fiber sensing in combustion liners – a survey")
        note_html = source_card.render_card_note(card)
        start = note_html.index("<pre>") + len("<pre>")
        end = note_html.index("</pre>")
        assert note_html[start:end].isascii()
        assert source_card.parse_card_note(note_html)["title_short"] == card["title_short"]

    def test_parse_rejects_a_note_with_no_pre_block(self):
        with pytest.raises(CliError) as exc:
            source_card.parse_card_note("<h1>Source card</h1><p>no data block</p>")
        assert exc.value.code == "invalid_card"

    def test_parse_rejects_the_wrong_schema_line(self):
        bad = "<h1>Source card</h1><p>x</p><pre>source-index/v1\n{}</pre>"
        with pytest.raises(CliError) as exc:
            source_card.parse_card_note(bad)
        assert exc.value.code == "invalid_card"

    def test_parse_rejects_json_that_does_not_parse(self):
        bad = "<h1>Source card</h1><p>x</p><pre>source-card/v1\n{not json}</pre>"
        with pytest.raises(CliError) as exc:
            source_card.parse_card_note(bad)
        assert exc.value.code == "invalid_card"

    def test_parse_rejects_json_that_is_not_an_object(self):
        bad = "<h1>Source card</h1><p>x</p><pre>source-card/v1\n[1, 2]</pre>"
        with pytest.raises(CliError):
            source_card.parse_card_note(bad)


# ---------------------------------------------------------------------------
# push_card, against a fake library and a fake annotations module
# ---------------------------------------------------------------------------

class FakeBackend:
    def __init__(self, items=None, children=None):
        self.items = list(items or [])
        self.children = dict(children or {})
        self.search_calls = []
        self.children_calls = []

    def search_items(self, query, *, item_type=None, limit=None, **kwargs):
        self.search_calls.append((query, item_type, limit))
        return list(self.items)

    def get_children(self, keys, *, item_type=None):
        self.children_calls.append((list(keys), item_type))
        return {key: self.children[key] for key in keys if key in self.children}


class FakeAnnotations:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self.update_result = "Successfully updated note."
        self.delete_result = "Successfully trashed note."
        self._next_key = 1

    def create_note(self, *, item_key, note_title, note_text, tags, ctx):
        key = f"NOTE{self._next_key:04d}"
        self._next_key += 1
        self.created.append((item_key, note_title, note_text, tags))
        return f"Successfully created note. Note key: {key}"

    def update_note(self, *, item_key, note_text, ctx, append=False):
        self.updated.append((item_key, note_text, append))
        return self.update_result

    def delete_note(self, *, item_key, ctx):
        self.deleted.append(item_key)
        return self.delete_result


def _install_backend(monkeypatch, backend):
    module = types.ModuleType("zotero_mcp.library")
    module.get_library_backend = lambda: backend
    _install(monkeypatch, "zotero_mcp.library", module)


def _install_annotations(monkeypatch, fake):
    module = types.ModuleType("zotero_mcp.tools.annotations")
    module.create_note = fake.create_note
    module.update_note = fake.update_note
    module.delete_note = fake.delete_note
    _install(monkeypatch, "zotero_mcp.tools.annotations", module)


def _note(key, html_text, added, *, deleted=0):
    return {"key": key, "data": {"note": html_text, "dateAdded": added, "deleted": deleted}}


class TestPushCard:
    def test_creates_a_note_when_there_is_none(self, monkeypatch):
        backend = FakeBackend(children={"ABCD1234": []})
        fake_ann = FakeAnnotations()
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, fake_ann)

        result = source_card.push_card("ABCD1234", VALID_CARD, ctx=None)

        assert result == {"item_key": "ABCD1234", "note_key": "NOTE0001",
                          "created": 1, "updated": 0, "trashed": 0}
        assert fake_ann.created[0][:2] == ("ABCD1234", "")
        assert backend.children_calls == [(["ABCD1234"], "note")]

    def test_updates_the_oldest_note_and_trashes_the_rest(self, monkeypatch):
        html_mid = source_card.render_card_note(_card(title_short="mid"))
        html_old = source_card.render_card_note(_card(title_short="old"))
        notes = [_note("NOTEMID01", html_mid, "2026-01-02"), _note("NOTEOLD01", html_old, "2026-01-01")]
        backend = FakeBackend(children={"ABCD1234": notes})
        fake_ann = FakeAnnotations()
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, fake_ann)

        result = source_card.push_card("ABCD1234", VALID_CARD, ctx=None)

        assert result == {"item_key": "ABCD1234", "note_key": "NOTEOLD01",
                          "created": 0, "updated": 1, "trashed": 1}
        assert fake_ann.updated == [("NOTEOLD01", source_card.render_card_note(VALID_CARD), False)]
        assert fake_ann.deleted == ["NOTEMID01"]

    def test_skips_a_trashed_card_note(self, monkeypatch):
        html_trashed = source_card.render_card_note(_card(title_short="gone"))
        notes = [_note("NOTEGONE1", html_trashed, "2026-01-01", deleted=1)]
        backend = FakeBackend(children={"ABCD1234": notes})
        fake_ann = FakeAnnotations()
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, fake_ann)

        result = source_card.push_card("ABCD1234", VALID_CARD, ctx=None)

        assert (result["created"], result["updated"], result["trashed"]) == (1, 0, 0)
        assert fake_ann.deleted == []

    def test_refuses_a_mismatched_item_key(self, monkeypatch):
        backend = FakeBackend(children={"ABCD1234": []})
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, FakeAnnotations())

        with pytest.raises(CliError) as exc:
            source_card.push_card("OTHRKEY1", VALID_CARD, ctx=None)
        assert exc.value.code == "invalid_card"
        assert backend.children_calls == []

    def test_refuses_an_invalid_card_before_any_backend_call(self, monkeypatch):
        backend = FakeBackend()
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, FakeAnnotations())

        with pytest.raises(CliError) as exc:
            source_card.push_card("ABCD1234", _card(kind="blog"), ctx=None)
        assert exc.value.code == "invalid_card"
        assert backend.children_calls == []

    def test_reports_a_failed_update(self, monkeypatch):
        html_old = source_card.render_card_note(VALID_CARD)
        notes = [_note("NOTEOLD01", html_old, "2026-01-01")]
        backend = FakeBackend(children={"ABCD1234": notes})
        fake_ann = FakeAnnotations()
        fake_ann.update_result = "Error: item is locked"
        _install_backend(monkeypatch, backend)
        _install_annotations(monkeypatch, fake_ann)

        with pytest.raises(CliError) as exc:
            source_card.push_card("ABCD1234", VALID_CARD, ctx=None)
        assert exc.value.code == "error"


# ---------------------------------------------------------------------------
# list_cards: grep, ranking, and the top-level-only scope of the backend call
# ---------------------------------------------------------------------------

def _item(key, *, parent=None):
    data = {"key": key, "itemType": "journalArticle"}
    if parent:
        data["parentItem"] = parent
    return {"key": key, "data": data}


class TestListCards:
    def test_lists_every_card_ranked_by_title_short_with_no_terms(self, monkeypatch):
        html_a = source_card.render_card_note(_card(item_key="AAAA1111", title_short="Beta title"))
        html_b = source_card.render_card_note(_card(item_key="BBBB2222", title_short="Alpha title"))
        backend = FakeBackend(
            items=[_item("AAAA1111"), _item("BBBB2222"), _item("CCCC3333")],
            children={
                "AAAA1111": [_note("N1", html_a, "x")],
                "BBBB2222": [_note("N2", html_b, "x")],
                "CCCC3333": [],
            },
        )
        _install_backend(monkeypatch, backend)

        result = source_card.list_cards(terms=None, regex=False, ctx=None)

        assert result["count"] == 2
        assert [c["item_key"] for c in result["cards"]] == ["BBBB2222", "AAAA1111"]
        assert backend.children_calls == [(["AAAA1111", "BBBB2222", "CCCC3333"], "note")]

    def test_asks_only_about_top_level_items(self, monkeypatch):
        html_a = source_card.render_card_note(_card(item_key="AAAA1111"))
        backend = FakeBackend(
            items=[_item("AAAA1111"), _item("ATTC0001", parent="AAAA1111")],
            children={"AAAA1111": [_note("N1", html_a, "x")]},
        )
        _install_backend(monkeypatch, backend)

        source_card.list_cards(terms=None, regex=False, ctx=None)

        [(keys, item_type)] = backend.children_calls
        assert keys == ["AAAA1111"]
        assert item_type == "note"

    def test_grep_matches_any_field_and_ranks_by_distinct_terms_matched(self, monkeypatch):
        card_a = _card(item_key="AAAA1111",
                       topics=["windback seal", "leakage rate", "axial clearance", "oil", "bearing"])
        card_b = _card(item_key="BBBB2222", title_short="Bearing sump sealing options",
                       topics=["bearing sump", "oil containment", "seal cost", "install time", "service life"],
                       scope="A survey of bearing sump sealing options.",
                       not_about="Nothing measurable here.", quantities=[],
                       synonyms=["windback"] + [f"syn {i}" for i in range(9)])
        backend = FakeBackend(
            items=[_item("AAAA1111"), _item("BBBB2222")],
            children={
                "AAAA1111": [_note("N1", source_card.render_card_note(card_a), "x")],
                "BBBB2222": [_note("N2", source_card.render_card_note(card_b), "x")],
            },
        )
        _install_backend(monkeypatch, backend)

        result = source_card.list_cards(terms=["windback", "leakage"], regex=False, ctx=None)

        assert result["count"] == 2
        assert result["cards"][0]["item_key"] == "AAAA1111"
        assert result["cards"][0]["hit"] == ["windback", "leakage"]
        assert result["cards"][1]["hit"] == ["windback"]

    def test_a_term_with_no_hit_on_any_card_is_dropped(self, monkeypatch):
        card_a = _card(item_key="AAAA1111")
        backend = FakeBackend(
            items=[_item("AAAA1111")],
            children={"AAAA1111": [_note("N1", source_card.render_card_note(card_a), "x")]},
        )
        _install_backend(monkeypatch, backend)

        result = source_card.list_cards(terms=["zqxnomatch"], regex=False, ctx=None)
        assert result == {"cards": [], "count": 0, "terms": ["zqxnomatch"]}

    def test_short_term_is_case_sensitive_and_whole_word(self, monkeypatch):
        card_a = _card(item_key="AAAA1111", quantities=["Cd"])
        backend = FakeBackend(
            items=[_item("AAAA1111")],
            children={"AAAA1111": [_note("N1", source_card.render_card_note(card_a), "x")]},
        )
        _install_backend(monkeypatch, backend)

        assert source_card.list_cards(terms=["cd"], regex=False, ctx=None)["count"] == 0
        assert source_card.list_cards(terms=["Cd"], regex=False, ctx=None)["count"] == 1

    def test_regex_term(self, monkeypatch):
        card_a = _card(item_key="AAAA1111", scope="leakage rises near 400 C at full speed.")
        backend = FakeBackend(
            items=[_item("AAAA1111")],
            children={"AAAA1111": [_note("N1", source_card.render_card_note(card_a), "x")]},
        )
        _install_backend(monkeypatch, backend)

        result = source_card.list_cards(terms=[r"\d+ C"], regex=True, ctx=None)
        assert result["count"] == 1

    def test_bad_regex_raises_before_any_backend_call(self, monkeypatch):
        backend = FakeBackend()
        _install_backend(monkeypatch, backend)

        with pytest.raises(CliError) as exc:
            source_card.list_cards(terms=["("], regex=True, ctx=None)
        assert exc.value.code == "bad_regex"
        assert backend.search_calls == []

    def test_a_note_that_is_not_a_readable_card_is_skipped(self, monkeypatch):
        backend = FakeBackend(
            items=[_item("AAAA1111")],
            children={"AAAA1111": [_note("N1", "<h1>Source card</h1><p>x</p>", "x")]},
        )
        _install_backend(monkeypatch, backend)

        assert source_card.list_cards(terms=None, regex=False, ctx=None)["count"] == 0


# ---------------------------------------------------------------------------
# CLI formatting: the compact line and the bulk-size guard
# ---------------------------------------------------------------------------

class TestCardLineAndBlock:
    def test_fits_without_truncation(self):
        card = {"item_key": "CARD0001", "kind": "paper", "title_short": "Short", "scope": "Also short."}
        assert cli_standalone._card_line(card, []) == "CARD0001 paper Short: Also short."

    def test_truncates_with_the_hit_note_kept_inside_the_cap(self):
        card = {"item_key": "CARD0001", "kind": "paper", "title_short": "x" * 100, "scope": "y" * 150}
        line = cli_standalone._card_line(card, ["leakage", "windback"])
        assert len(line) <= 200
        assert line.endswith("[hit: leakage, windback]")
        assert "..." in line

    def test_no_cards_message_with_and_without_terms(self):
        assert cli_standalone._format_index_cards({"cards": [], "terms": []}) == "No cards."
        assert cli_standalone._format_index_cards(
            {"cards": [], "terms": ["leakage"]}) == "No cards match leakage."

    def test_four_hundred_synthetic_cards_stay_under_the_markdown_cap(self):
        cards = [{
            "item_key": f"CARD{i:04d}",
            "kind": "paper",
            "title_short": f"Synthetic source {i} on windback seal leakage behaviour at speed",
            "scope": f"What synthetic source {i} claims about leakage near a windback seal under load.",
            "hit": ["leakage", "windback"],
        } for i in range(400)]
        text = cli_standalone._format_index_cards(
            {"cards": cards, "count": len(cards), "terms": ["leakage", "windback"], "expand": False})
        assert len(text) <= 80_000
        for line in text.splitlines():
            assert len(line) <= 200

    def test_expand_prints_the_full_card(self):
        card = {**VALID_CARD, "note_key": "NOTE0001", "hit": []}
        text = cli_standalone._format_index_cards(
            {"cards": [card], "terms": [], "expand": True})
        assert "NOTE0001" in text
        assert "topics" in text


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestCardsParsers:
    def test_cards_defaults(self):
        args = build_parser().parse_args(["index", "cards"])
        assert (args.command, args.subcommand) == ("index", "cards")
        assert (args.grep, args.regex, args.expand) == (None, False, False)
        assert getattr(args, "cards_subcommand", None) is None

    def test_cards_grep_and_expand(self):
        args = build_parser().parse_args(
            ["index", "cards", "--grep", "a,b", "--grep", "c", "--regex", "--expand"])
        assert args.grep == ["a,b", "c"]
        assert (args.regex, args.expand) == (True, True)

    def test_cards_push_defaults(self):
        args = build_parser().parse_args(["index", "cards", "push", "KEY1"])
        assert (args.cards_subcommand, args.key, args.from_file) == ("push", "KEY1", "-")

    def test_cards_push_from_file(self):
        args = build_parser().parse_args(["index", "cards", "push", "KEY1", "--from", "card.json"])
        assert args.from_file == "card.json"

    def test_cards_push_needs_a_key(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["index", "cards", "push"])

    @pytest.mark.parametrize("argv", [
        ["--json", "index", "cards"],
        ["index", "--json", "cards"],
        ["index", "cards", "--json"],
        ["--json", "index", "cards", "push", "KEY1"],
        ["index", "cards", "push", "KEY1", "--json"],
    ])
    def test_json_flag_works_anywhere(self, argv):
        assert build_parser().parse_args(argv).json_out is True

    def test_json_flag_before_the_command_survives_the_leaf_default(self):
        assert build_parser().parse_args(["--json", "index", "cards"]).json_out is True
        assert build_parser().parse_args(["index", "cards"]).json_out is False

    def test_schema_doc_names_cards_and_invalid_card(self):
        doc = cli_standalone.JSON_SCHEMA_DOC
        assert "index cards" in doc
        assert "index cards push" in doc
        assert "invalid_card" in doc


# ---------------------------------------------------------------------------
# CLI wiring: fakes the whole `source_card` module, the way the index tests fake
# `source_index`
# ---------------------------------------------------------------------------

def _install_fake_source_card(monkeypatch, *, list_result=None, push_result=None, validate_problems=None):
    calls = SimpleNamespace(list=[], push=[], validate=[])
    module = types.ModuleType("zotero_mcp.source_card")

    def list_cards(*, terms, regex, ctx):
        calls.list.append((terms, regex))
        return list_result

    def validate_card(card):
        calls.validate.append(card)
        return list(validate_problems or [])

    def push_card(parent_key, card, *, ctx):
        calls.push.append((parent_key, card))
        return push_result

    module.list_cards = list_cards
    module.validate_card = validate_card
    module.push_card = push_card
    _install(monkeypatch, "zotero_mcp.source_card", module)
    return calls


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
    monkeypatch.setattr(cli_standalone, "_keep_pymupdf_off_stdout", lambda: None)
    monkeypatch.setattr(pdf_source, "parent_key", lambda key: key)


def run_cli(monkeypatch, capsys, *argv):
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


class TestCliCardsList:
    def test_compact_json_keeps_only_the_compact_fields(self, monkeypatch, capsys, no_env):
        full_card = {**VALID_CARD, "note_key": "NOTE0001", "hit": ["leakage"]}
        _install_fake_source_card(
            monkeypatch, list_result={"cards": [full_card], "count": 1, "terms": ["leakage"]})

        code, body = run_json(monkeypatch, capsys, "--json", "index", "cards", "--grep", "leakage")

        assert code == 0
        data = body["data"]
        assert data["count"] == 1
        [card] = data["cards"]
        assert set(card) == {"item_key", "kind", "title_short", "scope", "hit"}
        assert data["terms"] == ["leakage"]

    def test_expand_keeps_the_full_card_and_note_key(self, monkeypatch, capsys, no_env):
        full_card = {**VALID_CARD, "note_key": "NOTE0001", "hit": []}
        _install_fake_source_card(monkeypatch, list_result={"cards": [full_card], "count": 1, "terms": []})

        code, body = run_json(monkeypatch, capsys, "--json", "index", "cards", "--expand")

        [card] = body["data"]["cards"]
        assert card["note_key"] == "NOTE0001"
        assert card["topics"] == VALID_CARD["topics"]

    def test_markdown_renders_the_compact_line(self, monkeypatch, capsys, no_env):
        full_card = {**VALID_CARD, "note_key": "NOTE0001", "hit": ["leakage"]}
        _install_fake_source_card(
            monkeypatch, list_result={"cards": [full_card], "count": 1, "terms": ["leakage"]})

        code, out, _err = run_cli(monkeypatch, capsys, "index", "cards", "--grep", "leakage")

        assert code == 0
        assert out.strip() == cli_standalone._card_line(full_card, ["leakage"])


class TestCliCardsPush:
    def test_push_success(self, monkeypatch, capsys, no_env):
        calls = _install_fake_source_card(monkeypatch, push_result={
            "item_key": "ABCD1234", "note_key": "NOTE0001", "created": 1, "updated": 0, "trashed": 0,
        })
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(VALID_CARD)))

        code, body = run_json(monkeypatch, capsys, "--json", "index", "cards", "push", "ABCD1234")

        assert code == 0
        assert body["data"]["note_key"] == "NOTE0001"
        assert calls.push == [("ABCD1234", VALID_CARD)]

    def test_push_reads_from_a_file(self, monkeypatch, capsys, no_env, tmp_path):
        calls = _install_fake_source_card(monkeypatch, push_result={
            "item_key": "ABCD1234", "note_key": "NOTE0001", "created": 1, "updated": 0, "trashed": 0,
        })
        path = tmp_path / "card.json"
        path.write_text(json.dumps(VALID_CARD))

        run_json(monkeypatch, capsys, "--json", "index", "cards", "push", "ABCD1234", "--from", str(path))

        assert calls.push[0][0] == "ABCD1234"

    def test_refuses_an_invalid_card_before_any_push(self, monkeypatch, capsys, no_env):
        calls = _install_fake_source_card(monkeypatch, validate_problems=["kind must be one of (...)"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_card(kind="blog"))))

        code, body = run_json(monkeypatch, capsys, "--json", "index", "cards", "push", "ABCD1234")

        assert code != 0
        assert body["error"]["code"] == "invalid_card"
        assert calls.push == []

    def test_reports_a_file_that_is_not_json(self, monkeypatch, capsys, no_env):
        _install_fake_source_card(monkeypatch)
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))

        code, body = run_json(monkeypatch, capsys, "--json", "index", "cards", "push", "ABCD1234")

        assert code != 0
        assert body["error"]["code"] == "invalid_card"


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

def test_importing_the_module_loads_neither_pymupdf_nor_fitz():
    code = (
        "import sys, importlib\n"
        "importlib.import_module('zotero_mcp.source_card')\n"
        "bad = [name for name in sys.modules if name.split('.')[0] in ('pymupdf', 'fitz')]\n"
        "sys.exit('loaded: ' + ', '.join(bad) if bad else 0)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
