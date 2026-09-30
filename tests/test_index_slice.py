"""`filter_index_pages` cuts a source index to a page range for `index show --pages`.

The fixture is the synthetic sample index (30 pages; S01 1-8, S02 9-15,
S03 16-30). `_index` bends it so that two sections share page 16, a fact sits
under a neighbour section, and a gap has no section, because those are the
cases where a section-based cut and a page-based cut disagree.
"""

import copy
import json
from pathlib import Path

import pytest

from zotero_mcp import cli_json
from zotero_mcp.index_slice import filter_index_pages

FIXTURE = Path(__file__).parent / "fixtures" / "source_index_sample.json"


def _index() -> dict:
    idx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # S02 runs one page into S03: page 16 belongs to two sections.
    idx["sections"][1]["end_page"] = 16
    base = idx["facts"][0]
    # Page 16, filed under S02 (the earlier of the two sections that share it).
    idx["facts"].append(dict(base, id="F0007", page=16, section_id="S02"))
    # Page 17 lies in S03 only, but the fact is filed under the neighbour S02.
    idx["facts"].append(dict(base, id="F0008", page=17, section_id="S02"))
    # A gap that belongs to no section.
    idx["gaps"].append({"id": "G02", "page": 12, "section_id": None,
                        "kind": "out-of-scope", "note": "page 12 is a foldout"})
    idx["x_reviewer_notes"] = {"round": 2, "open": ["check Fig. 7"]}
    idx["x_flags"] = ["keep-me"]
    return idx


def _ids(entries: list[dict]) -> list[str]:
    return [e["id"] for e in entries]


def test_page_filter_keeps_only_entries_on_the_pages():
    out = filter_index_pages(_index(), [18])
    assert _ids(out["facts"]) == ["F0004", "F0005"]
    assert _ids(out["tables_figures"]) == ["T01"]
    assert out["equations"] == []
    assert out["gaps"] == []
    assert _ids(out["sections"]) == ["S03"]


def test_each_page_array_is_filtered_on_its_own_page():
    out = filter_index_pages(_index(), [14, 20])
    assert _ids(out["facts"]) == ["F0006"]
    assert _ids(out["tables_figures"]) == ["T02"]
    assert _ids(out["equations"]) == ["E01"]
    assert _ids(out["gaps"]) == ["G01"]


def test_boundary_page_shared_by_two_sections_keeps_both_sections():
    out = filter_index_pages(_index(), [16])
    assert _ids(out["sections"]) == ["S02", "S03"]
    # Each neighbour is kept once; no other section leaks in.
    assert _ids(out["facts"]) == ["F0007"]


def test_page_next_to_the_boundary_keeps_one_section():
    idx = _index()
    assert _ids(filter_index_pages(idx, [15])["sections"]) == ["S02"]
    assert _ids(filter_index_pages(idx, [17])["sections"]) == ["S03"]


def test_fact_filed_under_a_neighbour_section_is_kept_by_its_page():
    idx = _index()
    out = filter_index_pages(idx, [17])
    # F0008 sits under S02, which does not meet page 17; the page decides.
    assert _ids(out["facts"]) == ["F0008"]
    assert out["facts"][0]["section_id"] == "S02"
    assert _ids(out["sections"]) == ["S03"]
    # And the same fact is gone from a slice of its filing section's pages.
    assert "F0008" not in _ids(filter_index_pages(idx, [9, 10, 11])["facts"])


def test_gap_with_null_section_id_is_kept_or_dropped_by_page():
    idx = _index()
    kept = filter_index_pages(idx, [12])
    assert _ids(kept["gaps"]) == ["G02"]
    assert kept["gaps"][0]["section_id"] is None
    assert filter_index_pages(idx, [13])["gaps"] == []


def test_a_page_list_is_not_a_range():
    out = filter_index_pages(_index(), [1, 30])
    assert _ids(out["sections"]) == ["S01", "S03"]


def test_order_and_repeats_in_the_page_list_do_not_matter():
    idx = _index()
    assert filter_index_pages(idx, [18, 6, 18, 6]) == filter_index_pages(idx, [6, 18])


def test_range_spanning_a_section_edge_keeps_both_sections():
    out = filter_index_pages(_index(), [8, 9])
    assert _ids(out["sections"]) == ["S01", "S02"]
    assert _ids(out["facts"]) == []


def test_schema_header_vocabulary_and_unknown_keys_are_kept_unchanged():
    idx = _index()
    # Page 30 holds no fact, gap, table or equation and no vocabulary page.
    out = filter_index_pages(idx, [30])
    assert out["schema"] == idx["schema"]
    assert out["header"] == idx["header"]
    assert out["vocabulary"] == idx["vocabulary"]
    assert len(out["vocabulary"]) == 3
    assert out["x_reviewer_notes"] == {"round": 2, "open": ["check Fig. 7"]}
    assert out["x_flags"] == ["keep-me"]
    assert set(out) == set(idx)
    assert out["facts"] == [] and out["tables_figures"] == []
    assert out["equations"] == [] and out["gaps"] == []


def test_input_is_not_changed():
    idx = _index()
    before = copy.deepcopy(idx)
    filter_index_pages(idx, [16, 17])
    assert idx == before


def test_output_shares_no_structure_with_the_input():
    idx = _index()
    before = copy.deepcopy(idx)
    out = filter_index_pages(idx, [6, 16])
    out["header"]["title"] = "changed"
    out["vocabulary"][0]["term"] = "changed"
    out["facts"][0]["statement"] = "changed"
    out["sections"][0]["title"] = "changed"
    out["x_reviewer_notes"]["open"].append("changed")
    out["x_flags"].append("changed")
    assert idx == before


def test_all_pages_gives_an_equal_copy():
    idx = _index()
    out = filter_index_pages(idx, list(range(1, 31)))
    assert out == idx
    assert out is not idx


def test_result_serialises_as_json():
    out = filter_index_pages(_index(), [12, 16])
    assert json.loads(json.dumps(out)) == out


@pytest.mark.parametrize("pages", [[], [0], [-3], [31], [5, 31], [5, 0], [1000]])
def test_bad_pages_is_raised_for_empty_low_and_high_pages(pages):
    with pytest.raises(cli_json.CliError) as err:
        filter_index_pages(_index(), pages)
    assert err.value.code == "bad_pages"


def test_bad_pages_leaves_the_input_unchanged():
    idx = _index()
    before = copy.deepcopy(idx)
    with pytest.raises(cli_json.CliError):
        filter_index_pages(idx, [31])
    assert idx == before


def test_the_first_and_last_page_are_valid():
    idx = _index()
    assert _ids(filter_index_pages(idx, [1])["sections"]) == ["S01"]
    assert _ids(filter_index_pages(idx, [30])["sections"]) == ["S03"]


@pytest.mark.parametrize("pages", [["3"], [3.0], [True], [None]])
def test_bad_pages_is_raised_for_a_page_that_is_not_an_integer(pages):
    with pytest.raises(cli_json.CliError) as err:
        filter_index_pages(_index(), pages)
    assert err.value.code == "bad_pages"


def test_no_page_count_in_the_header_skips_the_upper_check():
    idx = _index()
    del idx["header"]["page_count"]
    assert filter_index_pages(idx, [99])["facts"] == []
    with pytest.raises(cli_json.CliError) as err:
        filter_index_pages(idx, [0])
    assert err.value.code == "bad_pages"


def test_entries_without_a_usable_page_are_dropped():
    idx = _index()
    idx["facts"].append({"id": "F0009", "page": None})
    idx["facts"].append({"id": "F0010"})
    idx["sections"].append({"id": "S04", "title": "Odd"})
    out = filter_index_pages(idx, list(range(1, 31)))
    assert "F0009" not in _ids(out["facts"]) and "F0010" not in _ids(out["facts"])
    assert "S04" not in _ids(out["sections"])
