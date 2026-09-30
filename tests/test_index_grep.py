"""`index_grep` searches a source index for `index show --grep` and `index search`.

The indexes here are synthetic and small. Each test builds only the entries it needs, so a
failure names one rule: the match rule (ligatures, dashes, short terms, one field at a time),
the expansion rules (forms, symbols, broad terms), the rank and the limit, the lead projection,
and the page cut. The last tests guard the import: the index path must not load pymupdf.
"""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from zotero_mcp import cli_json, index_grep, text_match
from zotero_mcp.index_grep import (
    KINDS,
    LEAD_FIELDS,
    apply_filters,
    filter_index_terms,
    limit_index,
    parse_terms,
    project_index,
    vocabulary_on_pages,
)

SRC = Path(__file__).resolve().parents[1] / "src"
MARK = "zqxmark"  # a term that no fixture text contains by accident


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def fact(fid="F0001", page=1, **fields):
    entry = {
        "id": fid, "kind": "text", "statement": None, "quantity": None, "symbol": None, "value": None,
        "value_num": None, "value_max": None, "unit": None, "condition": None, "validity": None,
        "page": page, "printed_page": None, "section_id": "S01", "ref": None, "quote": None,
        "found_by": ["S01-reader"], "confidence": 5,
    }
    entry.update(fields)
    return entry


def vocab(term="widget", *, kind="term", symbol=None, meaning="a thing", variants=(), pages=(1,)):
    return {"term": term, "kind": kind, "symbol": symbol, "meaning": meaning,
            "variants": list(variants), "pages": list(pages)}


def figure(tid="T01", label="Figure 1", caption="A plot.", page=1):
    return {"id": tid, "label": label, "kind": "plot", "caption": caption, "page": page,
            "section_id": "S01", "extracted": "full", "fact_ids": []}


def equation(eid="E01", label="Eq. (1)", latex="x = y", variables=(), page=1, validity=None):
    return {"id": eid, "label": label, "page": page, "section_id": "S01", "latex": latex,
            "variables": list(variables), "validity": validity, "fact_ids": []}


def gap(gid="G01", kind="other", note="nothing", page=1):
    return {"id": gid, "page": page, "section_id": "S01", "kind": kind, "note": note}


def make_index(*, facts=(), vocabulary=(), tables_figures=(), equations=(), gaps=(), page_count=20):
    return {
        "schema": "source-index/v1",
        "header": {"item_key": "ABCD1234", "attachment_key": "WXYZ5678", "title": "A paper", "year": "2020",
                   "page_count": page_count, "authors": ["Doe, Jane"]},
        "vocabulary": list(vocabulary),
        "sections": [{"id": "S01", "title": "All", "path": "All", "level": 1, "start_page": 1,
                      "end_page": page_count}],
        "facts": list(facts),
        "tables_figures": list(tables_figures),
        "equations": list(equations),
        "gaps": list(gaps),
        "x_unknown": {"keep": ["me"]},
    }


def ids(entries):
    return [e.get("id", e.get("term")) for e in entries]


def grep(index, terms, **kwargs):
    """`filter_index_terms` with a term string: the parsed terms, then the subset."""
    regex = kwargs.get("regex", False)
    subset, report = filter_index_terms(index, parse_terms(terms, regex=regex), **kwargs)
    return subset, report


def hit_ids(index, terms, kind="facts", **kwargs):
    subset, _report = grep(index, terms, **kwargs)
    return ids(subset[kind])


# ---------------------------------------------------------------------------
# text_match: the moved rules
# ---------------------------------------------------------------------------

def test_compile_term_case_sensitive_and_word():
    pattern = text_match.compile_term("Cd", word=True, ignore_case=False)
    assert pattern.search("Cd = 0.47")
    assert not pattern.search("cd")
    assert not pattern.search("Cdw")
    assert text_match.compile_term("Cd").search("cd")


def test_compile_term_bad_regex_and_empty_carry_the_code():
    with pytest.raises(text_match.BadRegexError) as excinfo:
        text_match.compile_term("(", regex=True)
    assert excinfo.value.code == "bad_regex"
    with pytest.raises(text_match.BadRegexError):
        text_match.compile_term("  ")


def test_pdf_grep_reexports_the_text_match_rules():
    pdf_grep = pytest.importorskip("zotero_mcp.pdf_grep")
    assert pdf_grep.normalize_text is text_match.normalize_text
    assert pdf_grep.BadRegexError is text_match.BadRegexError
    assert pdf_grep.NORMALIZER_VERSION == 2
    for term, regex, word in (("carry-over", False, False), ("Cd", False, True), ("a.c", True, False)):
        wrapped = pdf_grep._compile_term(term, regex=regex, word=word)
        direct = text_match.compile_term(term, regex=regex, word=word)
        assert (wrapped.pattern, wrapped.flags) == (direct.pattern, direct.flags)


# ---------------------------------------------------------------------------
# Match rule
# ---------------------------------------------------------------------------

def test_ligature_matches_plain_term():
    index = make_index(facts=[
        fact("F1", statement="The coef\ufb01cient is small"),   # fi ligature
        fact("F2", statement="The coe\ufb03cient is large"),    # ffi ligature
        fact("F3", statement="Nothing here"),
    ])
    assert hit_ids(index, ["coefficient"]) == ["F1", "F2"]
    assert hit_ids(index, ["coef\ufb01cient"]) == ["F1", "F2"]  # a ligature in the term is expanded too


def test_unicode_dash_matches_hyphen_term():
    index = make_index(facts=[
        fact("F1", statement="a carry\u2010over of oil"),        # U+2010
        fact("F2", statement="a carry\u2212over of oil"),        # U+2212 minus
        fact("F3", statement="a carry-over of oil"),
        fact("F4", statement="nothing"),
    ])
    assert hit_ids(index, ["carry-over"]) == ["F1", "F2", "F3"]
    assert hit_ids(index, ["carry\u2010over"]) == ["F1", "F2", "F3"]


def test_carry_over_matches_carryover_and_carry_space_over():
    index = make_index(facts=[
        fact("F1", statement="oil carry-over"),
        fact("F2", statement="oil carryover"),
        fact("F3", statement="oil carry over"),
        fact("F4", statement="oil Carry-Over"),
        fact("F5", statement="oil carries over"),
    ])
    for term in ("carry-over", "carry over", "Carry-Over"):
        assert hit_ids(index, [term]) == ["F1", "F2", "F3", "F4"], term
    assert hit_ids(index, ["carryover"]) == ["F2"]                             # one word: no join to tolerate


def test_literal_match_ignores_case():
    index = make_index(facts=[fact("F1", quantity="Windback Leakage"), fact("F2", quantity="other")])
    assert hit_ids(index, ["windback leakage"]) == ["F1"]
    assert hit_ids(index, ["WINDBACK LEAKAGE"]) == ["F1"]


def test_short_term_is_a_case_sensitive_whole_word():
    index = make_index(facts=[
        fact("F1", statement="Cd = 0.47"),
        fact("F2", statement="the value of cd"),
        fact("F3", statement="Cdw is the width"),
        fact("F4", statement="a (Cd) of 0.5"),
        fact("F5", statement="ACd"),
    ])
    assert hit_ids(index, ["Cd"]) == ["F1", "F4"]


def test_three_character_term_ignores_case_and_matches_inside_words():
    index = make_index(facts=[fact("F1", statement="the CDW value"), fact("F2", statement="a cdwx tag")])
    assert hit_ids(index, ["Cdw"]) == ["F1", "F2"]


_FIELD_CASES = [
    ("facts", "statement"), ("facts", "quantity"), ("facts", "symbol"), ("facts", "value"),
    ("facts", "unit"), ("facts", "condition"), ("facts", "validity"), ("facts", "ref"), ("facts", "quote"),
    ("vocabulary", "term"), ("vocabulary", "symbol"), ("vocabulary", "variants"), ("vocabulary", "meaning"),
    ("tables_figures", "label"), ("tables_figures", "caption"),
    ("equations", "label"), ("equations", "latex"), ("equations", "validity"),
    ("equations", "variables.symbol"), ("equations", "variables.meaning"),
    ("gaps", "kind"), ("gaps", "note"),
]


def _entry_with(kind, field):
    """One entry of ``kind`` that holds MARK in ``field`` only, and a plain decoy."""
    if kind == "facts":
        return fact("F1", **{field: MARK}), fact("F2", statement="plain")
    if kind == "vocabulary":
        value = [MARK] if field == "variants" else MARK
        return vocab(**{"term": "aaa", "meaning": "plain", "symbol": None, field: value}), vocab("bbb")
    if kind == "tables_figures":
        return figure("T1", **{field: MARK}), figure("T2", label="Figure 9", caption="plain")
    if kind == "equations":
        if field.startswith("variables."):
            name = field.split(".")[1]
            variables = [{"symbol": "a", "meaning": "plain", "unit": None, name: MARK}]
            return equation("E1", variables=variables), equation("E2", latex="plain")
        return equation("E1", **{field: MARK}), equation("E2", latex="plain")
    return gap("G1", **{field: MARK}), gap("G2", kind="other", note="plain")


@pytest.mark.parametrize("kind,field", _FIELD_CASES, ids=[f"{k}.{f}" for k, f in _FIELD_CASES])
def test_each_field_of_each_kind_matches_alone(kind, field):
    target, decoy = _entry_with(kind, field)
    index = make_index(**{kind: [target, decoy]})
    subset, report = grep(index, [MARK])
    assert len(subset[kind]) == 1
    assert subset[kind][0]["hit"] == [MARK]
    assert report["matched"][kind] == 1 and report["total"][kind] == 2
    assert sum(report["matched"].values()) == 1  # no other kind matched
    assert report["term_counts"] == {MARK: 1}


def test_ids_and_bookkeeping_never_match():
    index = make_index(
        facts=[fact(f"F{MARK}", section_id=f"S{MARK}", found_by=[f"{MARK}-reader"], kind=MARK,
                    value_num=424242, value_max=None, confidence=5, page=7)],
        vocabulary=[vocab("aaa", kind=MARK, pages=[424242])],
        tables_figures=[dict(figure(f"T{MARK}"), kind=MARK, extracted=MARK, fact_ids=[MARK])],
        equations=[dict(equation(f"E{MARK}"), fact_ids=[MARK])],
        gaps=[gap(f"G{MARK}")],
    )
    for term in (MARK, "424242", "reader"):
        subset, report = grep(index, [term])
        assert sum(report["matched"].values()) == 0, term
        assert all(subset[kind] == [] for kind in KINDS)
    # The same number in the value field does match.
    index["facts"].append(fact("F2", value="424242"))
    assert hit_ids(index, ["424242"]) == ["F2"]


def test_numbers_in_a_field_are_matched_as_text():
    index = make_index(facts=[fact("F1", value="5.34"), fact("F2", value="2"), fact("F3", value=5.34)])
    assert hit_ids(index, ["5.34"]) == ["F1", "F3"]


def test_no_match_across_the_field_join():
    index = make_index(facts=[
        fact("F1", quantity="carry", condition="over"),
        fact("F2", quantity="carry", condition="carry over"),
        fact("F3", quantity="carry over"),
    ])
    # F1 has the words in two fields: no hit. "\s" matches the "\x1f" join, so a plain search of the
    # joined text would hit here. F2 and F3 keep the words inside one field.
    assert hit_ids(index, ["carry over"]) == ["F2", "F3"]
    assert hit_ids(index, ["carry-over"]) == ["F2", "F3"]
    assert hit_ids(index, ["carry"]) == ["F1", "F2", "F3"]


def test_a_spanning_hit_does_not_hide_a_real_hit_in_a_later_field():
    # The first match in the joined text is "aaa\x1fbbb". The real one sits at the end of the second field.
    index = make_index(facts=[fact("F1", quantity="aaa", condition="bbb aaa bbb"), fact("F2", quantity="aaa bbb x")])
    assert hit_ids(index, ["aaa bbb"]) == ["F1", "F2"]


def test_a_short_term_does_not_join_two_neighbour_fields():
    index = make_index(facts=[fact("F1", quantity="C", condition="d"), fact("F2", quantity="Cd")])
    assert hit_ids(index, ["Cd"]) == ["F2"]


def test_regex_ignores_case():
    index = make_index(facts=[fact("F1", quantity="Leakage rate"), fact("F2", quantity="LEAKY seal"), fact("F3")])
    assert hit_ids(index, [r"leak(age|y)"], regex=True) == ["F1", "F2"]
    assert hit_ids(index, [r"^LEAKAGE"], regex=True) == ["F1"]


def test_regex_anchors_and_dots_stay_inside_one_field():
    index = make_index(facts=[
        fact("F1", quantity="carry", condition="over"),
        fact("F2", quantity="x over"),
    ])
    assert hit_ids(index, [r"^over"], regex=True) == ["F1"]        # the start of the second field
    assert hit_ids(index, [r"carry.over"], regex=True) == []       # a dot does not cross the join
    assert hit_ids(index, [r"over$"], regex=True) == ["F1", "F2"]


def test_literal_terms_are_escaped():
    index = make_index(facts=[fact("F1", statement="see f(x) here"), fact("F2", statement="see fx here")])
    assert hit_ids(index, ["f(x)"]) == ["F1"]


def test_needle_test_agrees_with_the_plain_regex_on_odd_letters():
    """The substring test before the regex must never drop a hit that ``re.IGNORECASE`` finds."""
    texts = ["300 \u212a", "\u212aelvin", "\u0130stanbul \u0131ron", "Mu\u017fic", "KELVIN", "plain text",
             "\u00b5m gap", "Stra\u00dfe"]
    terms = ["300 k", "istanbul", "iron", "music", "kelvin", "text", "mu", "strasse", "\u00b5m", "\u017f", "k"]
    for term in terms:
        matcher = index_grep._Matcher(term, regex=False)
        pattern = text_match.compile_term(term) if len(term) >= 3 else text_match.compile_term(
            term, word=True, ignore_case=False)
        for text in texts:
            normal = text_match.normalize_text(text)
            hay = index_grep._Hay([normal])
            assert matcher.match(hay) == bool(pattern.search(normal)), (term, text)


# ---------------------------------------------------------------------------
# Terms
# ---------------------------------------------------------------------------

def test_parse_terms_splits_on_commas_strips_and_drops_empty_parts():
    assert parse_terms(["a, b ,,c ,"], regex=False) == ["a", "b", "c"]
    assert parse_terms(["brush seal, bristle"], regex=False) == ["brush seal", "bristle"]


def test_parse_terms_repeats_join_in_order():
    assert parse_terms(["a,b", "c", "d, e"], regex=False) == ["a", "b", "c", "d", "e"]


def test_parse_terms_drops_duplicates_by_normalised_casefold_and_keeps_the_first():
    assert parse_terms(["Leakage", "leakage, LEAKAGE"], regex=False) == ["Leakage"]
    assert parse_terms(["carry\u2010over", "carry-over"], regex=False) == ["carry\u2010over"]
    assert parse_terms(["coef\ufb01cient", "coefficient"], regex=False) == ["coef\ufb01cient"]


def test_parse_terms_regex_keeps_the_comma_and_the_case():
    assert parse_terms(["a,b", "a,b", "A,B"], regex=True) == ["a,b", "A,B"]
    assert parse_terms(["x{1,3}"], regex=True) == ["x{1,3}"]


def test_parse_terms_empty_list_is_bad_grep():
    for values in ([], [""], [" , ,"], [","]):
        with pytest.raises(cli_json.CliError) as excinfo:
            parse_terms(values, regex=False)
        assert excinfo.value.code == "bad_grep", values
    with pytest.raises(cli_json.CliError) as excinfo:
        parse_terms(["  "], regex=True)
    assert excinfo.value.code == "bad_grep"


def test_check_terms_bad_regex_code():
    index_grep.check_terms(["fine", "also fine"], regex=False)
    index_grep.check_terms(["("], regex=False)  # a literal parenthesis is escaped
    with pytest.raises(cli_json.CliError) as excinfo:
        index_grep.check_terms(["ok", "("], regex=True)
    assert excinfo.value.code == "bad_regex"
    assert "(" in str(excinfo.value)


def test_a_bad_regex_fails_the_search_with_bad_regex():
    index = make_index(facts=[fact("F1", statement="text")])
    for call in (
        lambda: filter_index_terms(index, ["["], regex=True),
        lambda: apply_filters(index, terms=["(unclosed"], regex=True),
    ):
        with pytest.raises(cli_json.CliError) as excinfo:
            call()
        assert excinfo.value.code == "bad_regex"


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def _leakage_vocabulary():
    return [vocab("leakage flow", symbol="m_leak, ml", variants=["seepage", "loss rate"], meaning="flow that escapes")]


def test_expansion_adds_the_term_the_variants_and_the_symbol_parts():
    index = make_index(
        vocabulary=_leakage_vocabulary(),
        facts=[
            fact("F1", statement="The seepage was 2 g/s"),
            fact("F2", quantity="loss rate"),
            fact("F3", symbol="m_leak"),
            fact("F4", symbol="ml"),
            fact("F5", statement="about the flow of leakage flow"),
            fact("F6", statement="unrelated"),
        ],
    )
    subset, report = grep(index, ["leakage"], expand=True)
    assert report["expanded_terms"] == ["leakage flow", "seepage", "loss rate", "m_leak"]
    assert report["expanded_total"] == 4
    assert report["expanded_symbols"] == ["ml"]
    assert report["broad_terms"] == []
    hits = {e["id"]: e["hit"] for e in subset["facts"]}
    assert hits == {
        "F1": ["seepage"], "F2": ["loss rate"], "F3": ["m_leak"], "F4": ["ml"], "F5": ["leakage", "leakage flow"],
    }
    # The vocabulary entry itself hits: the user term, then its own forms, at most three.
    assert ids(subset["vocabulary"]) == ["leakage flow"]
    assert subset["vocabulary"][0]["hit"] == ["leakage", "leakage flow", "seepage"]


def test_a_short_form_matches_symbol_fields_only():
    index = make_index(
        vocabulary=[vocab("swirl number", symbol="S, n", variants=["swirl"])],
        facts=[
            fact("F1", statement="the n value of s and S"),          # text: never
            fact("F2", symbol="S"),                                   # exact
            fact("F3", symbol="s"),                                   # case differs
            fact("F4", symbol="S, n"),                                # two parts
            fact("F5", symbol="Sn"),                                  # not equal to a part
            fact("F6", unit="n", quantity="S"),                       # not the symbol field
        ],
        equations=[
            equation("E1", variables=[{"symbol": "n", "meaning": "count", "unit": None}]),
            equation("E2", variables=[{"symbol": "a", "meaning": "n", "unit": None}]),
        ],
    )
    subset, report = grep(index, ["swirl number"], expand=True)
    assert report["expanded_symbols"] == ["S", "n"]
    assert report["expanded_terms"] == ["swirl"]
    hits = {e["id"]: e["hit"] for e in subset["facts"]}
    assert hits == {"F2": ["S"], "F4": ["S", "n"]}
    assert ids(subset["equations"]) == ["E1"]
    assert subset["equations"][0]["hit"] == ["n"]
    # The vocabulary entry matches its own symbol parts too.
    assert ids(subset["vocabulary"]) == ["swirl number"]


def test_a_short_form_symbol_field_of_a_vocabulary_entry_matches():
    index = make_index(vocabulary=[
        vocab("swirl number", symbol="S", variants=["swirl"]),
        vocab("entropy", symbol="S", meaning="specific entropy"),
        vocab("other", symbol="s"),
    ])
    subset, report = grep(index, ["swirl"], expand=True)
    assert report["expanded_symbols"] == ["S"]
    assert ids(subset["vocabulary"]) == ["swirl number", "entropy"]
    assert subset["vocabulary"][1]["hit"] == ["S"]


def test_a_numeric_form_is_dropped():
    index = make_index(
        vocabulary=[vocab("leakage", variants=["0.47", "100 %", "seepage", "1.5-3"])],
        facts=[fact("F1", statement="value 0.47 here"), fact("F2", statement="seepage")],
    )
    subset, report = grep(index, ["leak"], expand=True)
    assert report["expanded_terms"] == ["leakage", "seepage"]
    assert ids(subset["facts"]) == ["F2"]


def test_a_form_equal_to_a_user_term_is_dropped():
    index = make_index(vocabulary=[vocab("Leakage", variants=["leakage", "LEAKAGE", "seepage"])])
    _subset, report = grep(index, ["leakage"], expand=True)
    assert report["expanded_terms"] == ["seepage"]


def test_a_spelling_of_a_user_term_is_not_a_form_and_the_hit_lists_the_term_once():
    """"brush-seal" has the pattern key of the user term "brush seal": it adds no form and no second hit."""
    index = make_index(
        vocabulary=[vocab("brush seal", variants=["brush-seal", "Brush  Seal", "bristle"])],
        facts=[fact("F1", statement="a brush seal here"), fact("F2", statement="a brush-seal here"),
               fact("F3", statement="bristle tip")],
    )
    subset, report = grep(index, ["brush seal"], expand=True)
    assert report["expanded_terms"] == ["bristle"] and report["expanded_total"] == 1
    assert "brush-seal" not in report["expanded_terms"]
    assert {e["id"]: e["hit"] for e in subset["facts"]} == {
        "F1": ["brush seal"], "F2": ["brush seal"], "F3": ["bristle"],
    }


def test_forms_with_one_pattern_key_are_one_form():
    index = make_index(vocabulary=[vocab(
        "leakage", variants=["back flow", "back-flow", "Back  Flow", "backflow", "tipx", "-tipx"])])
    subset, report = grep(index, ["leakage"], expand=True)
    # The pattern joins words with [\s\-]*: the first three are one pattern. "backflow" has no join, and an
    # edge hyphen is literal, so "-tipx" is not "tipx".
    assert report["expanded_terms"] == ["back flow", "backflow", "tipx", "-tipx"]
    assert report["expanded_total"] == 4
    index["facts"] = [fact("F1", statement="a back-flow path")]
    subset, _report = grep(index, ["leakage"], expand=True)
    assert subset["facts"][0]["hit"] == ["back flow"]


def test_25_entries_expand_and_26_go_to_broad_terms():
    def vocabulary(count):
        return [vocab(f"seal type {n}", variants=[f"form number {n}"]) for n in range(count)]

    facts = [fact("F1", statement="form number 3 was tested"), fact("F2", statement="seal type text")]
    subset, report = grep(make_index(vocabulary=vocabulary(25), facts=facts), ["seal"], expand=True)
    assert report["broad_terms"] == []
    assert report["expanded_total"] == 50
    assert ids(subset["facts"]) == ["F1", "F2"]
    assert subset["facts"][0]["hit"] == ["form number 3"]

    subset, report = grep(make_index(vocabulary=vocabulary(26), facts=facts), ["seal"], expand=True)
    assert report["broad_terms"] == ["seal"]
    assert report["expanded_total"] == 0 and report["expanded_terms"] == []
    assert ids(subset["facts"]) == ["F2"]                       # only the direct hit
    assert report["term_counts"]["seal"] == 27                   # 26 vocabulary entries + F2


def test_a_broad_term_does_not_stop_the_other_terms_from_expanding():
    vocabulary = [vocab(f"seal type {n}") for n in range(30)] + [vocab("leakage", variants=["seepage"])]
    index = make_index(vocabulary=vocabulary, facts=[fact("F1", statement="seepage")])
    subset, report = grep(index, ["seal", "leakage"], expand=True)
    assert report["broad_terms"] == ["seal"]
    assert report["expanded_terms"] == ["seepage"]
    assert ids(subset["facts"]) == ["F1"]


def test_expansion_reads_term_variants_and_symbol_parts_but_not_the_meaning():
    index = make_index(vocabulary=[
        vocab("alpha", meaning="the leakage of oil", variants=["a1"], symbol="q_a"),   # meaning only
        vocab("beta", variants=["leakage path", "route"]),
        vocab("gamma", symbol="m_leak, x", variants=["gam"]),
    ])
    _subset, report = grep(index, ["leakage"], expand=True)     # only beta lends: alpha has it in the meaning
    assert report["expanded_terms"] == ["beta", "leakage path", "route"]
    _subset, report = grep(index, ["m_leak"], expand=True)      # a symbol part of gamma
    assert report["expanded_terms"] == ["gamma", "gam"]
    assert report["expanded_symbols"] == ["x"]


def test_the_vocabulary_before_the_pages_cut_drives_expansion():
    index = make_index(
        vocabulary=[vocab("leakage", variants=["seepage"], pages=[5])],      # not on page 2
        facts=[fact("F1", page=2, statement="seepage rate"), fact("F2", page=5, statement="seepage again")],
    )
    out, report = apply_filters(index, pages=[2], terms=["leakage"], expand=True, fields="full")
    assert ids(out["facts"]) == ["F1"]
    assert out["facts"][0]["hit"] == ["seepage"]
    assert out["vocabulary"] == []                                            # the cut still applies
    assert report["expanded_terms"] == ["seepage"]
    assert report["total"]["vocabulary"] == 0


def test_report_cap_80_with_expanded_total():
    variants = [f"form{n:03d}" for n in range(100)]
    index = make_index(
        vocabulary=[vocab("leakage", variants=variants)],
        facts=[fact("F1", statement="only form095 here")],
    )
    subset, report = grep(index, ["leakage"], expand=True)
    assert report["expanded_total"] == 100
    assert report["expanded_terms"] == variants[:80]
    assert ids(subset["facts"]) == ["F1"]                                    # a form past the cap still matches
    assert subset["facts"][0]["hit"] == ["form095"]


def test_a_typical_expansion_is_reported_in_full():
    """46 forms (the windback "leakage" case) all fit under the report cap, so none is hidden."""
    variants = [f"form{n:02d}" for n in range(46)]              # the term "leakage" is the user term: not a form
    index = make_index(vocabulary=[vocab("leakage", variants=variants)])
    _subset, report = grep(index, ["leakage"], expand=True)
    assert report["expanded_total"] == 46 and report["expanded_terms"] == variants


def test_the_first_form_is_kept_in_vocabulary_order():
    index = make_index(vocabulary=[
        vocab("leakage", variants=["seepage", "loss"]),
        vocab("leakage rate", variants=["Seepage", "drip", "loss"]),
    ])
    _subset, report = grep(index, ["leakage"], expand=True)
    assert report["expanded_terms"] == ["seepage", "loss", "leakage rate", "drip"]
    assert report["expanded_total"] == 4


def test_hit_lists_user_terms_then_expanded_forms_then_symbols_at_most_three():
    index = make_index(
        vocabulary=[vocab("leakage", variants=["seepage", "drip", "weep", "ooze", "leak"], symbol="q")],
        facts=[fact("F1", statement="leakage and Leakage rate, seepage, drip, weep, ooze, leak", symbol="q")],
    )
    subset, _report = grep(index, ["leak", "rate"], expand=True)
    assert subset["facts"][0]["hit"] == ["leak", "rate", "leakage"]
    subset, _report = grep(index, ["leak", "rate", "drip", "weep"], expand=True)     # user terms alone fill it
    assert subset["facts"][0]["hit"] == ["leak", "rate", "drip"]
    subset, _report = grep(index, ["leakage"], expand=True)
    assert subset["facts"][0]["hit"] == ["leakage", "seepage", "drip"]
    index["facts"][0]["statement"] = "seepage only"
    subset, _report = grep(index, ["leakage"], expand=True)
    assert subset["facts"][0]["hit"] == ["seepage", "q"]                     # the symbol comes last
    index["facts"][0]["statement"] = "seepage, drip, weep"
    subset, _report = grep(index, ["leakage"], expand=True)
    assert subset["facts"][0]["hit"] == ["seepage", "drip", "weep"]          # the symbol no longer fits


def test_expand_finds_nothing_more_when_the_vocabulary_has_no_bridge():
    index = make_index(vocabulary=[vocab("other")], facts=[fact("F1", statement="leaks")])
    subset, report = grep(index, ["leakage"], expand=True)
    assert subset["facts"] == []
    assert report["expanded_total"] == 0 and report["expanded_symbols"] == []


def test_expand_works_with_a_regex_term():
    index = make_index(vocabulary=[vocab("leakage", variants=["seepage"])],
                       facts=[fact("F1", statement="seepage")])
    subset, report = grep(index, [r"^leak"], regex=True, expand=True)
    assert report["expanded_terms"] == ["leakage", "seepage"]
    assert ids(subset["facts"]) == ["F1"]


def test_vocabulary_argument_replaces_the_expansion_source():
    index = make_index(vocabulary=[vocab("leakage")], facts=[fact("F1", statement="seepage")])
    source = [vocab("leakage", variants=["seepage"])]
    subset, _report = filter_index_terms(index, ["leakage"], expand=True, vocabulary=source)
    assert ids(subset["facts"]) == ["F1"]


# ---------------------------------------------------------------------------
# Rank and limit
# ---------------------------------------------------------------------------

def _ranked_index():
    facts = [fact(f"F{n}", statement="seepage") for n in range(1, 5)]              # expanded-only hits
    facts.append(fact("F5", statement="leakage late in the index"))              # a user-term hit
    return make_index(vocabulary=[vocab("leakage", variants=["seepage"])], facts=facts)


def test_a_user_term_hit_late_in_the_index_beats_an_expanded_only_hit_early():
    out, report = apply_filters(_ranked_index(), terms=["leakage"], expand=True, fields="lead", limit=1)
    assert ids(out["facts"]) == ["F5"]
    assert report["matched"]["facts"] == 5 and report["returned"]["facts"] == 1
    assert report["truncated"]["facts"] == 4


def test_output_keeps_index_order_after_the_rank_cut():
    out, _report = apply_filters(_ranked_index(), terms=["leakage"], expand=True, fields="lead", limit=3)
    # Kept by rank: F5 (user hit), then F1 and F2 (earliest of the expanded-only tier); shown in index order.
    assert ids(out["facts"]) == ["F1", "F2", "F5"]


def test_more_distinct_hits_rank_higher_inside_a_tier():
    index = make_index(facts=[
        fact("F1", statement="alpha"), fact("F2", statement="alpha beta"), fact("F3", statement="beta"),
        fact("F4", statement="alpha beta gamma"),
    ])
    out, _report = apply_filters(index, terms=["alpha", "beta", "gamma"], fields="lead", limit=2)
    assert ids(out["facts"]) == ["F2", "F4"]
    out, _report = apply_filters(index, terms=["alpha", "beta", "gamma"], fields="lead", limit=3)
    assert ids(out["facts"]) == ["F1", "F2", "F4"]      # equal hits: the earlier position wins


def test_a_user_term_hit_beats_expanded_only_hits_however_many_they_are():
    """The hit list stops at HIT_CAP, so three expanded hits and thirty tie: the tier decides first."""
    forms = ["seepage", "drip", "weep", "ooze", "trickle"]
    facts = [fact(f"F{n}", statement=", ".join(forms)) for n in range(1, 5)]        # 5 expanded hits, capped at 3
    facts.append(fact("F5", statement="leakage only"))                              # one user-term hit, last
    index = make_index(vocabulary=[vocab("leakage", variants=forms)], facts=facts)
    out, report = apply_filters(index, terms=["leakage"], expand=True, fields="lead", limit=1)
    assert ids(out["facts"]) == ["F5"]
    assert len(out["facts"][0]["hit"]) == 1
    full, _report = grep(index, ["leakage"], expand=True)
    assert [len(e["hit"]) for e in full["facts"]] == [index_grep.HIT_CAP] * 4 + [1]
    out, report = apply_filters(index, terms=["leakage"], expand=True, fields="lead", limit=2)
    assert ids(out["facts"]) == ["F1", "F5"]                     # the user hit, then the earliest expanded-only hit
    assert report["truncated"]["facts"] == 3


def test_the_rank_ties_at_the_hit_cap_and_the_earlier_position_wins():
    facts = [fact("F1", statement="aaa bbb ccc"), fact("F2", statement="aaa bbb ccc ddd"),
             fact("F3", statement="aaa bbb ccc ddd eee"), fact("F4", statement="aaa")]
    index = make_index(facts=facts)
    terms = ["aaa", "bbb", "ccc", "ddd", "eee"]
    out, _report = apply_filters(index, terms=terms, fields="lead", limit=2)
    assert ids(out["facts"]) == ["F1", "F2"]                     # three hits or more all count 3: index order
    assert all(entry["hit"] == ["aaa", "bbb", "ccc"] for entry in out["facts"])
    out, _report = apply_filters(index, terms=terms, fields="lead", limit=3)
    assert ids(out["facts"]) == ["F1", "F2", "F3"]               # F4 (one hit) ranks below the saturated three


def test_a_user_term_hit_stays_first_in_a_capped_hit_list():
    forms = ["seepage", "drip", "weep", "ooze"]
    index = make_index(
        vocabulary=[vocab("leakage", variants=forms)],
        facts=[fact("F1", statement="leakage " + " ".join(forms))],
    )
    subset, _report = grep(index, ["leakage"], expand=True)
    assert subset["facts"][0]["hit"] == ["leakage", "seepage", "drip"]
    assert index_grep._rank(subset["facts"][0], {"leakage"}, 0)[0] == 0


def test_truncated_counts_by_kind():
    index = make_index(
        facts=[fact(f"F{n}", statement=MARK) for n in range(7)],
        vocabulary=[vocab(f"term{n}", meaning=MARK) for n in range(5)],
        gaps=[gap("G1", note=MARK)],
    )
    out, report = apply_filters(index, terms=[MARK], fields="lead", limit=3)
    assert report["matched"] == {"facts": 7, "vocabulary": 5, "tables_figures": 0, "equations": 0, "gaps": 1}
    assert report["returned"] == {"facts": 3, "vocabulary": 3, "tables_figures": 0, "equations": 0, "gaps": 1}
    assert report["truncated"] == {"facts": 4, "vocabulary": 2, "tables_figures": 0, "equations": 0, "gaps": 0}
    assert len(out["facts"]) == 3 and len(out["vocabulary"]) == 3 and len(out["gaps"]) == 1


def test_limit_zero_means_no_cap():
    index = make_index(facts=[fact(f"F{n}", statement=MARK) for n in range(60)])
    out, report = apply_filters(index, terms=[MARK], fields="lead", limit=0)
    assert len(out["facts"]) == 60
    assert report["returned"]["facts"] == 60 and report["truncated"]["facts"] == 0
    assert report["limit"] == 0


def test_limit_larger_than_the_matches_cuts_nothing():
    index = make_index(facts=[fact(f"F{n}", statement=MARK) for n in range(3)])
    out, report = apply_filters(index, terms=[MARK], fields="lead", limit=40)
    assert len(out["facts"]) == 3 and report["truncated"]["facts"] == 0


def test_without_terms_the_limit_keeps_the_first_entries_by_index_position():
    index = make_index(facts=[fact(f"F{n}") for n in range(1, 6)], vocabulary=[vocab(f"t{n}") for n in range(4)])
    out, report = apply_filters(index, fields="lead", limit=2)
    assert ids(out["facts"]) == ["F1", "F2"]
    assert ids(out["vocabulary"]) == ["t0", "t1"]
    assert all("hit" not in entry for entry in out["facts"])
    assert report["matched"]["facts"] == 5 and report["truncated"]["facts"] == 3
    assert report["terms"] == [] and report["term_counts"] == {}


def test_limit_index_alone_ranks_by_hit_lists():
    index = make_index(facts=[
        dict(fact("F1"), hit=["seepage"]), dict(fact("F2"), hit=["leakage", "seepage"]), dict(fact("F3"), hit=["leakage"]),
    ])
    subset, returned, truncated = limit_index(index, 1, terms=["leakage"])
    assert ids(subset["facts"]) == ["F2"]
    assert returned["facts"] == 1 and truncated["facts"] == 2
    subset, _returned, _truncated = limit_index(index, 1)                     # no terms: index position
    assert ids(subset["facts"]) == ["F1"]
    assert set(returned) == set(KINDS) and set(truncated) == set(KINDS)


def test_a_bad_limit_is_a_value_error():
    index = make_index()
    for limit in (-1, 1.5, "3", None, True):
        with pytest.raises(ValueError):
            limit_index(index, limit)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

def _one_of_each():
    return make_index(
        facts=[fact("F1", quantity="gap", value="0.1", unit="mm", condition="cold", ref="Table 2",
                    statement="A long statement.", quote="a quote", symbol="c")],
        vocabulary=[vocab("clearance", symbol="c", variants=["gap"], meaning="radial gap", pages=[3, 4])],
        tables_figures=[figure("T1", "Table 2", "Clearance gap by size")],
        equations=[equation("E1", "Eq. (1)", "c = a - b", variables=[{"symbol": "c", "meaning": "gap", "unit": "mm"}])],
        gaps=[gap("G1", "other", "a gap note")],
    )


def test_lead_key_sets_per_kind_plus_hit():
    out, _report = apply_filters(_one_of_each(), terms=["gap"], fields="lead", limit=40)
    for kind in KINDS:
        assert len(out[kind]) == 1, kind
        assert set(out[kind][0]) == set(LEAD_FIELDS[kind]) | {"hit"}, kind
    assert LEAD_FIELDS == {
        "facts": ("id", "page", "kind", "quantity", "value", "unit", "condition", "ref", "section_id"),
        "vocabulary": ("term", "kind", "symbol", "variants", "pages"),
        "tables_figures": ("id", "page", "label", "caption"),
        "equations": ("id", "page", "label", "latex"),
        "gaps": ("id", "page", "kind"),
    }
    assert out["facts"][0] == {
        "id": "F1", "page": 1, "kind": "text", "quantity": "gap", "value": "0.1", "unit": "mm",
        "condition": "cold", "ref": "Table 2", "section_id": "S01", "hit": ["gap"],
    }
    assert out["equations"][0]["latex"] == "c = a - b"
    assert "statement" not in out["facts"][0] and "quote" not in out["facts"][0]


def test_lead_omits_a_key_that_is_null_or_missing_and_full_keeps_it():
    entry = fact("F1", quantity="gap", value="0.1", unit=None, condition=None)   # unit, condition: null
    del entry["ref"]                                                              # ref: missing
    index = make_index(facts=[entry])
    lead = project_index(index, "lead")["facts"][0]
    assert lead == {"id": "F1", "page": 1, "kind": "text", "quantity": "gap", "value": "0.1", "section_id": "S01"}
    full = project_index(index, "full")["facts"][0]
    assert full["unit"] is None and full["condition"] is None and "ref" not in full
    lead, _report = apply_filters(index, terms=["gap"], fields="lead", limit=40)
    assert "unit" not in lead["facts"][0] and "condition" not in lead["facts"][0]
    assert lead["facts"][0]["hit"] == ["gap"]                                     # hit is never null
    full, _report = apply_filters(index, terms=["gap"], fields="full", limit=40)
    assert full["facts"][0]["unit"] is None and full["facts"][0]["condition"] is None


def test_lead_omits_null_keys_of_every_kind_and_keeps_values_that_are_not_null():
    index = make_index(
        facts=[fact("F1", quantity="q", value=0, unit="", condition=None)],          # 0 and "" are not null
        vocabulary=[vocab("clearance", symbol=None, variants=[], pages=[3])],        # [] is not null
        tables_figures=[figure("T1", label=None, caption="A plot.")],
        equations=[equation("E1", label=None, latex="x = y")],
        gaps=[gap("G1", kind="other")],
    )
    out = project_index(index, "lead")
    assert out["facts"][0] == {"id": "F1", "page": 1, "kind": "text", "quantity": "q", "value": 0, "unit": "",
                               "section_id": "S01"}
    assert out["vocabulary"][0] == {"term": "clearance", "kind": "term", "variants": [], "pages": [3]}
    assert out["tables_figures"][0] == {"id": "T1", "page": 1, "caption": "A plot."}
    assert out["equations"][0] == {"id": "E1", "page": 1, "latex": "x = y"}
    assert out["gaps"][0] == {"id": "G1", "page": 1, "kind": "other"}
    assert all("hit" not in record for kind in KINDS for record in out[kind])          # no terms: no hit


def test_lead_passes_header_sections_schema_and_unknown_keys_in_full():
    index = _one_of_each()
    out = project_index(index, "lead")
    for key in ("schema", "header", "sections", "x_unknown"):
        assert out[key] == index[key], key
    assert list(out) == list(index)                                            # same key order


def test_full_returns_the_records_unchanged_plus_hit():
    index = _one_of_each()
    out, _report = apply_filters(index, terms=["gap"], fields="full", limit=40)
    for kind in KINDS:
        assert out[kind] == [{**index[kind][0], "hit": ["gap"]}], kind


def test_the_projection_shares_nothing_with_the_input():
    index = _one_of_each()
    for fields in ("lead", "full"):
        out, _report = apply_filters(index, terms=["gap"], fields=fields, limit=40)
        out["vocabulary"][0]["variants"].append("mutated")
        out["header"]["authors"].append("mutated")
        out["x_unknown"]["keep"].append("mutated")
        out["sections"][0]["title"] = "mutated"
    assert index == _one_of_each()


def test_an_unknown_fields_value_is_a_value_error():
    with pytest.raises(ValueError):
        project_index(make_index(), "medium")
    with pytest.raises(ValueError):
        apply_filters(make_index(), fields="medium")


# ---------------------------------------------------------------------------
# Pages and the chain
# ---------------------------------------------------------------------------

def test_vocabulary_pages_cut_keeps_entries_that_meet_the_pages_and_drops_those_without():
    index = make_index(vocabulary=[
        vocab("a", pages=[1, 2]), vocab("b", pages=[5]), vocab("c", pages=[]), dict(vocab("d"), pages=None),
        {k: v for k, v in vocab("e").items() if k != "pages"}, vocab("f", pages=[2, 9]),
    ])
    assert ids(vocabulary_on_pages(index, [2, 3])["vocabulary"]) == ["a", "f"]
    assert ids(vocabulary_on_pages(index, [5])["vocabulary"]) == ["b"]
    assert vocabulary_on_pages(index, [7])["vocabulary"] == []
    assert len(index["vocabulary"]) == 6                                       # the input is untouched


def test_pages_cut_every_array_and_the_vocabulary_and_total_counts_after_it():
    index = make_index(
        facts=[fact("F1", page=1, statement=MARK), fact("F2", page=2, statement=MARK), fact("F3", page=2)],
        vocabulary=[vocab("a", meaning=MARK, pages=[1]), vocab("b", meaning=MARK, pages=[2]), vocab("c", pages=[2])],
        tables_figures=[figure("T1", page=1, caption=MARK), figure("T2", page=2, caption=MARK)],
        equations=[equation("E1", page=2, label=MARK)],
        gaps=[gap("G1", page=3, note=MARK)],
    )
    out, report = apply_filters(index, pages=[2], terms=[MARK], fields="lead", limit=40)
    assert ids(out["facts"]) == ["F2"] and ids(out["vocabulary"]) == ["b"]
    assert ids(out["tables_figures"]) == ["T2"] and ids(out["equations"]) == ["E1"] and out["gaps"] == []
    assert report["total"] == {"facts": 2, "vocabulary": 2, "tables_figures": 1, "equations": 1, "gaps": 0}
    assert report["matched"] == {"facts": 1, "vocabulary": 1, "tables_figures": 1, "equations": 1, "gaps": 0}
    assert report["pages"] == [2]
    assert [s["id"] for s in out["sections"]] == ["S01"]


def test_bad_pages_surface_with_the_bad_pages_code():
    index = make_index(page_count=10)
    for pages in ([11], [0], []):
        with pytest.raises(cli_json.CliError) as excinfo:
            apply_filters(index, pages=pages, fields="lead")
        assert excinfo.value.code == "bad_pages", pages


def test_without_pages_the_vocabulary_is_not_cut():
    index = make_index(vocabulary=[vocab("a", pages=[]), vocab("b", pages=[5])])
    out, report = apply_filters(index, fields="lead", limit=40)
    assert ids(out["vocabulary"]) == ["a", "b"]
    assert report["pages"] is None and report["total"]["vocabulary"] == 2


def test_filters_are_anded_pages_terms_and_limit():
    index = make_index(facts=[
        fact("F1", page=1, statement=MARK), fact("F2", page=2, statement=MARK), fact("F3", page=2, statement="no"),
        fact("F4", page=2, statement=MARK),
    ])
    out, report = apply_filters(index, pages=[2], terms=[MARK], fields="lead", limit=1)
    assert ids(out["facts"]) == ["F2"]
    assert report["total"]["facts"] == 3 and report["matched"]["facts"] == 2 and report["truncated"]["facts"] == 1


def test_report_shape_and_key_order():
    index = make_index(facts=[fact("F1", statement=MARK)], vocabulary=[vocab("a", variants=[MARK])])
    _out, report = apply_filters(index, terms=[MARK], regex=False, expand=True, fields="lead", limit=40, pages=None)
    assert list(report) == [
        "terms", "regex", "expand", "fields", "limit", "pages", "expanded_terms", "expanded_total",
        "expanded_symbols", "broad_terms", "term_counts", "total", "matched", "returned", "truncated",
    ]
    assert report["terms"] == [MARK] and report["regex"] is False and report["expand"] is True
    assert report["fields"] == "lead" and report["limit"] == 40 and "section" not in report
    for name in ("total", "matched", "returned", "truncated"):
        assert list(report[name]) == list(KINDS), name


def test_term_counts_count_entries_of_all_kinds_for_each_user_term():
    index = make_index(
        facts=[fact("F1", statement="alpha beta"), fact("F2", statement="alpha")],
        vocabulary=[vocab("alpha")],
        gaps=[gap("G1", note="beta")],
    )
    _out, report = apply_filters(index, terms=["alpha", "beta", "gamma"], fields="lead")
    assert report["term_counts"] == {"alpha": 3, "beta": 2, "gamma": 0}


def test_input_is_not_mutated():
    index = make_index(
        vocabulary=[vocab("leakage", variants=["seepage"], pages=[1, 2], symbol="q, ml")],
        facts=[fact(f"F{n}", page=1 + n % 3, statement="seepage and leakage", symbol="ml") for n in range(8)],
        tables_figures=[figure(caption="leakage plot")],
        equations=[equation(variables=[{"symbol": "ml", "meaning": "leakage", "unit": None}])],
        gaps=[gap(note="leakage")],
    )
    before = copy.deepcopy(index)
    for kwargs in (
        dict(terms=["leakage"], expand=True, fields="full", limit=3),
        dict(terms=["leakage"], expand=True, fields="lead", limit=0, pages=[1, 2]),
        dict(terms=[r"leak\w+"], regex=True, fields="lead", limit=2),
        dict(fields="full", limit=0),
    ):
        apply_filters(index, **kwargs)
        assert index == before, kwargs
    filter_index_terms(index, ["leakage"], expand=True)
    limit_index(index, 1, terms=["leakage"])
    vocabulary_on_pages(index, [1])
    project_index(index, "lead")
    assert index == before


def test_no_terms_returns_the_whole_index_within_the_limit():
    index = make_index(facts=[fact("F1"), fact("F2")], gaps=[gap()])
    out, report = apply_filters(index, fields="full", limit=40)
    assert ids(out["facts"]) == ["F1", "F2"] and len(out["gaps"]) == 1
    assert report["matched"] == report["total"] == report["returned"]


def test_constants():
    assert KINDS == ("facts", "vocabulary", "tables_figures", "equations", "gaps")
    assert index_grep.MAX_EXPAND_ENTRIES == 25 and index_grep.REPORT_EXPANDED_CAP == 80
    assert index_grep.HIT_CAP == 3 and index_grep.SHOW_LIMIT == 40 and index_grep.SEARCH_LIMIT == 10


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", ["zotero_mcp.index_grep", "zotero_mcp.text_match"])
def test_importing_the_module_loads_neither_pymupdf_nor_fitz(module):
    code = (
        "import sys, importlib\n"
        f"importlib.import_module({module!r})\n"
        "bad = [name for name in sys.modules if name.split('.')[0] in ('pymupdf', 'fitz')]\n"
        "sys.exit('loaded: ' + ', '.join(bad) if bad else 0)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, result.stderr
