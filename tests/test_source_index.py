"""Tests for zotero_mcp.source_index: validate, render, parse, push and show.

Every Zotero call is mocked. The fake below stands in for the library backend
(``get_children``) and for the note and tag tool functions, and keeps notes in
memory, so push and show run their real logic against a store that behaves like
Zotero for the calls they make.
"""

import copy
import json
import random
import re
from pathlib import Path

import pytest

from zotero_mcp import source_index as si

FIXTURE = Path(__file__).parent / "fixtures" / "source_index_sample.json"
PARENT = "PARENT01"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def sample_index() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def with_facts(index: dict, facts: list[dict]) -> dict:
    """The index with these facts; fact_ids that point at a dropped fact are dropped too."""
    out = copy.deepcopy(index)
    out["facts"] = facts
    keep = {f["id"] for f in facts}
    for name in ("tables_figures", "equations"):
        for entry in out[name]:
            entry["fact_ids"] = [i for i in entry["fact_ids"] if i in keep]
    return out


def small_index(n: int = 3) -> dict:
    index = sample_index()
    return with_facts(index, index["facts"][:n])


def big_index(n_facts: int, seed: int = 7) -> dict:
    """A valid index with n_facts facts of realistic size, non-ASCII text and markup characters."""
    rng = random.Random(seed)
    index = sample_index()
    template = index["facts"][1]
    facts = []
    for i in range(1, n_facts + 1):
        page = 1 + i % 30
        facts.append(dict(
            template,
            id=f"F{i:04d}",
            statement=f"Fact {i}: the hole pitch p/d = {rng.random() * 9:.3f} & M < 2 at 10⁻³ Pa·s",
            quote="The measured value " + " ".join(rng.choice(["is", "was", "of", "at", "the"]) for _ in range(25)),
            value=f"{rng.random() * 100:.2f}",
            value_num=rng.random() * 100,
            page=page,
            section_id=f"S0{1 + page // 11}",
        ))
    return with_facts(index, facts)


def wide_index(n_vocab: int, n_gaps: int, n_facts: int, meaning_len: int = 60, seed: int = 11) -> dict:
    """A valid index with many vocabulary entries and gaps and few facts (the shape of a live glossary-heavy source)."""
    rng = random.Random(seed)
    index = with_facts(sample_index(), sample_index()["facts"][:n_facts])
    words = ["film", "hole", "liner", "swirl", "mixing", "plenum", "jet", "wall", "flow", "µm"]
    index["vocabulary"] = [
        {"term": f"term {i} <x> & y", "kind": ("term", "symbol", "acronym", "unit")[i % 4],
         "symbol": None if i % 4 else f"s{i}",
         "meaning": " ".join(rng.choice(words) for _ in range(meaning_len // 5))[:meaning_len].ljust(meaning_len, "."),
         "variants": [f"variant {i}a", f"variant {i}b"] if i % 3 == 0 else [],
         "pages": [1 + i % 30]}
        for i in range(n_vocab)
    ]
    index["gaps"] = [
        {"id": f"G{i:04d}", "page": 1 + i % 30, "section_id": "S02" if i % 5 else None, "kind": "figure-values",
         "note": f"Fig. {i} curves have no data labels; values cannot be read from text."}
        for i in range(n_gaps)
    ]
    return index


def block_of_parts(notes: list[str]) -> list[dict]:
    """The decoded JSON slice of every part."""
    out = []
    for note in notes:
        [block] = si._pre_blocks(note)
        out.append(json.loads(block.split("\n", 1)[1]))
    return out


def note_editor(note_html: str, *, attrs: bool = False, br: bool = False, entities: bool = False) -> str:
    """What the Zotero note editor does to stored HTML, as far as the parser cares."""
    out = note_html
    if attrs:  # a re-serialised note carries attributes; the next step strips them again
        out = re.sub(r"<(pre|td|th|h1|h2|table)>", r'<\1 class="x" data-k="1">', out)
    out = re.sub(r"<(\w+)\s[^>]*>", r"<\1>", out)                          # strip attributes
    out = re.sub(r"<(td|th)>(.*?)</\1>", r"<\1><p>\2</p></\1>", out, flags=re.DOTALL)  # cells hold paragraphs
    if br:  # the newline after line 1 of the pre block becomes a <br>
        out = re.sub(r"(<pre>[^\n]*)\n", r"\1<br>", out)
    if entities:
        out = re.sub(r"<pre>(.*?)</pre>",
                     lambda m: "<pre>" + m.group(1).replace('"', "&quot;").replace("'", "&#39;") + "</pre>",
                     out, flags=re.DOTALL)
    out = out.replace("<pre>", "<pre><code>").replace("</pre>", "</code></pre>")
    return f'<div data-schema-version="9">{out}</div>'


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_sample_fixture_is_valid_and_has_the_agreed_shape():
    index = sample_index()
    assert si.validate_index(index) == []
    assert list(index) == list(si.TOP_LEVEL_KEYS)
    assert (len(index["sections"]), len(index["facts"]), len(index["tables_figures"]),
            len(index["equations"]), len(index["gaps"])) == (3, 6, 2, 1, 1)


def test_gap_with_null_section_is_valid():
    index = sample_index()
    index["gaps"][0]["section_id"] = None
    assert si.validate_index(index) == []


@pytest.mark.parametrize("bad", [None, [], "index", 3])
def test_non_object_is_invalid(bad):
    assert si.validate_index(bad) == ["index must be a JSON object"]


@pytest.mark.parametrize("key", si.TOP_LEVEL_KEYS)
def test_missing_top_level_key(key):
    index = sample_index()
    del index[key]
    problems = si.validate_index(index)
    assert any("missing top-level key" in p and key in p for p in problems)


def test_wrong_schema_value():
    index = sample_index()
    index["schema"] = "source-index/v0"
    assert any("schema" in p for p in si.validate_index(index))


@pytest.mark.parametrize("name", ["sections", "facts", "tables_figures", "equations", "gaps"])
def test_duplicate_id(name):
    index = sample_index()
    index[name].append(copy.deepcopy(index[name][0]))
    problems = si.validate_index(index)
    assert any("duplicate id" in p and name in p and index[name][0]["id"] in p for p in problems)


def test_entry_without_id():
    index = sample_index()
    del index["facts"][2]["id"]
    assert any("facts[#2] has no id" in p for p in si.validate_index(index))


@pytest.mark.parametrize("name", ["facts", "tables_figures", "equations", "gaps"])
@pytest.mark.parametrize("bad_page", [0, 31, -4])
def test_page_out_of_range(name, bad_page):
    index = sample_index()
    index[name][0]["page"] = bad_page
    problems = si.validate_index(index)
    assert any(name in p and "outside 1..30" in p and str(bad_page) in p for p in problems)


@pytest.mark.parametrize("bad_page", ["7", 7.5, None, True])
def test_page_must_be_an_integer(bad_page):
    index = sample_index()
    index["facts"][0]["page"] = bad_page
    assert any("facts[F0001]" in p and "integer" in p for p in si.validate_index(index))


def test_section_and_vocabulary_pages_are_checked():
    index = sample_index()
    index["sections"][2]["end_page"] = 99
    index["vocabulary"][0]["pages"] = [5, 0]
    problems = si.validate_index(index)
    assert any("sections[S03]" in p and "outside 1..30" in p for p in problems)
    assert any("vocabulary[effusion cooling]" in p and "outside 1..30" in p for p in problems)


@pytest.mark.parametrize("bad", [None, 0, -3, "30"])
def test_header_page_count_must_be_positive_integer(bad):
    index = sample_index()
    index["header"]["page_count"] = bad
    assert any("page_count" in p for p in si.validate_index(index))


@pytest.mark.parametrize("name", ["facts", "tables_figures", "equations", "gaps"])
def test_section_id_must_resolve(name):
    index = sample_index()
    index[name][0]["section_id"] = "S99"
    problems = si.validate_index(index)
    assert any(name in p and "section_id" in p and "S99" in p for p in problems)


@pytest.mark.parametrize("name", ["facts", "tables_figures", "equations"])
def test_section_id_may_not_be_null_outside_gaps(name):
    index = sample_index()
    index[name][0]["section_id"] = None
    assert any(name in p and "section_id" in p for p in si.validate_index(index))


@pytest.mark.parametrize("name", ["tables_figures", "equations"])
def test_fact_ids_must_resolve(name):
    index = sample_index()
    index[name][0]["fact_ids"] = ["F0004", "F9999"]
    problems = si.validate_index(index)
    assert any(name in p and "fact_id" in p and "F9999" in p for p in problems)


def test_arrays_must_hold_objects():
    index = sample_index()
    index["facts"].append("not an object")
    index["gaps"] = {"id": "G01"}
    problems = si.validate_index(index)
    assert any("facts[#6] must be an object" in p for p in problems)
    assert any("gaps must be an array" in p for p in problems)


# ---------------------------------------------------------------------------
# Render and parse
# ---------------------------------------------------------------------------

def test_render_layout_of_a_single_note():
    index = small_index(3)
    notes = si.render_index_notes(index)
    assert len(notes) == 1
    note = notes[0]
    assert note.startswith("<h1>Source index</h1>")
    assert "<p>" in note                                 # keeps create_note from re-wrapping the HTML
    assert note.count("<pre>") == 1 and note.endswith("</pre>")
    block = note.split("<pre>", 1)[1].removesuffix("</pre>")
    line1, line2 = block.split("\n")
    assert line1 == "source-index/v1 part 1/1 build sample-20260929-01"
    assert "\n" not in line2 and ": " not in line2      # compact JSON on one line
    for label in ("<h2>Header</h2>", "<h2>Vocabulary</h2>", "<h2>Sections</h2>", "<h2>Facts</h2>",
                  "<h2>Tables and figures</h2>", "<h2>Equations</h2>", "<h2>Gaps</h2>"):
        assert label in note
    assert set(json.loads(line2)) == set(si.TOP_LEVEL_KEYS)


def test_vocabulary_and_gap_tables_are_compact():
    note = si.render_index_notes(small_index(3))[0]
    assert "<h2>Vocabulary</h2><table><tbody><tr><th>term</th><th>kind</th><th>meaning</th></tr>" in note
    assert "<h2>Gaps</h2><table><tbody><tr><th>page</th><th>kind</th><th>note</th></tr>" in note
    # variants are joined by ", " into the meaning cell
    assert "(also: full-coverage film cooling, effusion)</td>" in note
    assert "see data block" not in note


def test_round_trip_three_facts():
    index = small_index(3)
    assert si.parse_index_notes(si.render_index_notes(index)) == index


def test_round_trip_full_sample():
    index = sample_index()
    assert si.parse_index_notes(si.render_index_notes(index)) == index


def test_round_trip_no_facts():
    index = small_index(0)
    notes = si.render_index_notes(index)
    assert len(notes) == 1
    assert si.parse_index_notes(notes) == index


@pytest.fixture(scope="module")
def big():
    index = big_index(2000)
    return index, si.render_index_notes(index)


def test_round_trip_2000_facts_in_several_parts(big):
    index, notes = big
    assert len(notes) >= 2
    assert all(len(n) <= si.DEFAULT_MAX_CHARS for n in notes)
    assert si.parse_index_notes(notes) == index


def test_parts_are_titled_and_only_part_one_holds_header_and_sections(big):
    index, notes = big
    n = len(notes)
    assert notes[0].startswith("<h1>Source index</h1>")
    seen_ids = []
    for k, (note, data) in enumerate(zip(notes, block_of_parts(notes)), start=1):
        if k > 1:
            assert note.startswith(f"<h1>Source index (part {k} of {n})</h1>")
            assert "<h2>Header</h2>" not in note and "<h2>Sections</h2>" not in note
            assert data["header"] == {} and data["sections"] == []
        else:
            assert data["header"] == index["header"] and data["sections"] == index["sections"]
        [block] = si._pre_blocks(note)
        assert block.split("\n")[0] == f"source-index/v1 part {k}/{n} build {index['header']['build_id']}"
        assert set(data) == set(si.TOP_LEVEL_KEYS)
        seen_ids += [f["id"] for f in data["facts"]]
    # every fact exactly once, in order: no fact split or repeated across parts
    assert seen_ids == [f["id"] for f in index["facts"]]


def test_round_trip_2000_vocabulary_500_gaps_3_facts():
    index = wide_index(2000, 500, 3, meaning_len=150)
    notes = si.render_index_notes(index)
    assert len(notes) >= 2
    assert all(len(n) <= si.DEFAULT_MAX_CHARS for n in notes)
    assert si.parse_index_notes(notes) == index
    slices = block_of_parts(notes)
    assert sum(1 for s in slices if s["vocabulary"]) >= 2        # the glossary spans parts
    for name in ("vocabulary", "gaps", "facts"):                  # each entry once, in order
        joined = [e.get("id") or e.get("term") for s in slices for e in s[name]]
        assert joined == [e.get("id") or e.get("term") for e in index[name]]


def test_live_shaped_index_fits_part_one():
    """3 facts, 450 vocabulary entries of about 200 characters and 90 gaps: part 1 alone used to overflow."""
    index = wide_index(450, 90, 3, meaning_len=200)
    notes = si.render_index_notes(index, max_chars=190_000)
    assert all(len(n) <= 190_000 for n in notes)
    assert si.parse_index_notes(notes) == index
    # the tables are no bigger than the data they show
    for note in notes:
        view = note.split("</p>", 1)[1].rsplit("<pre>", 1)[0]
        assert len(view) <= 1.5 * len(note.rsplit("<pre>", 1)[1])


def test_every_packed_block_can_span_parts_in_pack_order():
    index = wide_index(120, 90, 3, meaning_len=150)
    index["tables_figures"] = [
        dict(index["tables_figures"][0], id=f"T{i:03d}", caption="c" * 300) for i in range(60)
    ] + index["tables_figures"][1:]
    index["equations"] = [dict(index["equations"][0], id=f"E{i:03d}") for i in range(60)]
    notes = si.render_index_notes(index, max_chars=20_000)
    assert all(len(n) <= 20_000 for n in notes)
    assert si.parse_index_notes(notes) == index
    slices = block_of_parts(notes)
    order = ("tables_figures", "equations", "vocabulary", "gaps", "facts")
    for name in order[:4]:
        assert sum(1 for s in slices if s[name]) >= 2, name
    # greedy fill: the last block of a part never comes after the first block of the next part
    used = [[order.index(name) for name in order if s[name]] for s in slices]
    used = [u for u in used if u]
    assert all(a[-1] <= b[0] for a, b in zip(used, used[1:]))


def test_tables_are_dropped_when_they_would_outgrow_the_data():
    """Bare facts: each row is mostly markup, so the tables would be several times the JSON."""
    index = small_index(0)
    index["facts"] = [{"id": f"F{i:04d}", "page": 1 + i % 30, "section_id": "S01"} for i in range(200)]
    notes = si.render_index_notes(index)
    assert len(notes) == 1
    assert "<h2>Facts</h2><p>200 entries, see data block.</p>" in notes[0]
    assert "<h2>Facts</h2><table>" not in notes[0]
    assert si.parse_index_notes(notes) == index


def test_tables_are_dropped_when_one_entry_would_not_fit_a_part_with_them():
    index = small_index(3)
    index["facts"][1]["statement"] = "x" * 20_000        # its row and its JSON together are over one part
    notes = si.render_index_notes(index, max_chars=30_000)
    assert all(len(n) <= 30_000 for n in notes)
    assert si.parse_index_notes(notes) == index
    slices = block_of_parts(notes)
    [huge] = [k for k, s in enumerate(slices) if any(f["id"] == index["facts"][1]["id"] for f in s["facts"])]
    assert "<h2>Facts</h2><p>" in notes[huge] and "entries, see data block.</p>" in notes[huge]
    assert "<table>" not in notes[huge].split("<h2>Facts</h2>", 1)[1]
    # a part that still has room keeps its tables
    assert "<h2>Facts</h2><table>" in notes[0] or "<h2>Vocabulary</h2><table>" in notes[0]


def test_sections_table_is_dropped_before_part_one_is_given_up():
    index = small_index(0)
    index["vocabulary"] = index["tables_figures"] = index["equations"] = index["gaps"] = []
    index["sections"] = [
        {"id": f"S{i:02d}", "title": "t" * 100, "path": str(i), "level": 1, "start_page": 1, "end_page": 30}
        for i in range(1, 60)
    ]
    with_tables = si.render_index_notes(index)
    assert len(with_tables) == 1 and "<h2>Sections</h2><table>" in with_tables[0]
    # The sizing assumes a wide part number, so a limit a little over the real size makes the tables go first.
    tight = si.render_index_notes(index, max_chars=len(with_tables[0]) + 2)
    assert len(tight) == 1 and len(tight[0]) < len(with_tables[0])
    assert "<h2>Sections</h2><p>59 entries, see data block.</p>" in tight[0]
    assert "<h2>Header</h2><table>" in tight[0]                           # the header table stays
    assert si.parse_index_notes(tight) == index
    with pytest.raises(ValueError, match="header and sections alone need"):
        si.render_index_notes(index, max_chars=1_000)


def test_parse_ignores_part_order(big):
    index, notes = big
    shuffled = list(notes)
    random.Random(3).shuffle(shuffled)
    assert si.parse_index_notes(shuffled) == index


@pytest.mark.parametrize("max_chars", [12_000, 40_000, 190_000])
def test_every_part_fits_max_chars(max_chars):
    index = big_index(300)
    notes = si.render_index_notes(index, max_chars=max_chars)
    assert all(len(n) <= max_chars for n in notes)
    assert si.parse_index_notes(notes) == index
    if max_chars == 12_000:
        assert len(notes) > 5


def test_render_fails_when_part_one_or_a_fact_cannot_fit():
    with pytest.raises(ValueError, match="alone need"):
        si.render_index_notes(sample_index(), max_chars=500)
    huge = small_index(3)
    huge["facts"][1]["statement"] = "x" * 50_000
    with pytest.raises(ValueError, match="more than one part"):
        si.render_index_notes(huge, max_chars=30_000)


def test_no_later_part_is_ever_empty():
    """A part exists only to hold an entry: sweep the limit across every packing boundary."""
    for n_facts in (0, 1, 5):
        index = with_facts(wide_index(120, 40, 3), big_index(n_facts)["facts"] if n_facts else [])
        laid_out = 0
        for max_chars in range(6_000, 40_000, 311):
            try:
                notes = si.render_index_notes(index, max_chars=max_chars)
            except ValueError:
                continue
            laid_out += 1
            assert all(len(n) <= max_chars for n in notes)
            for k, data in enumerate(block_of_parts(notes)[1:], start=2):
                assert any(data[name] for name in ("tables_figures", "equations", "vocabulary", "gaps", "facts")), \
                    f"part {k} of {len(notes)} holds no entry at max_chars={max_chars}"
        assert laid_out > 50


@pytest.mark.parametrize("block, entry", [
    ("vocabulary", {"term": "t", "kind": "term", "symbol": None, "meaning": "m" * 20_000, "variants": [],
                    "pages": [1]}),
    ("gaps", {"id": "G9", "page": 1, "section_id": None, "kind": "other", "note": "n" * 20_000}),
    ("tables_figures", {"id": "T9", "label": "l", "kind": "table", "caption": "c" * 20_000, "page": 1,
                        "section_id": "S01", "extracted": "full", "fact_ids": []}),
    ("equations", {"id": "E9", "label": None, "page": 1, "section_id": "S01", "latex": "l" * 20_000,
                   "variables": [], "validity": None, "fact_ids": []}),
])
def test_render_fails_for_any_block_entry_too_big_for_a_part(block, entry):
    index = small_index(3)
    index[block].append(entry)
    with pytest.raises(ValueError, match=rf"{block} entry .* more than one part"):
        si.render_index_notes(index, max_chars=10_000)


def test_push_reports_a_block_entry_too_big_for_a_part_as_invalid_index(zotero, monkeypatch):
    monkeypatch.setattr(si, "DEFAULT_MAX_CHARS", 10_000)
    index = small_index(3)
    index["gaps"].append({"id": "G9", "page": 1, "section_id": None, "kind": "other", "note": "n" * 20_000})
    with pytest.raises(si.SourceIndexError, match="gaps entry") as info:
        push(index)
    assert info.value.code == "invalid_index" and zotero.writes() == []


def test_render_needs_a_build_id():
    index = small_index(3)
    index["header"]["build_id"] = ""
    with pytest.raises(ValueError, match="build_id"):
        si.render_index_notes(index)


def test_unknown_keys_survive_in_one_part_and_in_many():
    index = small_index(3)
    index["x_provenance"] = {"tool": "reader", "rounds": [1, 2]}
    index["header"]["reviewer"] = "someone"
    index["vocabulary"][0]["note"] = "extra"
    index["facts"][1]["extra_field"] = {"nested": ["a", None, 2.5]}
    index["sections"][0]["x"] = True
    assert si.parse_index_notes(si.render_index_notes(index)) == index

    many = big_index(200)
    many["x_provenance"] = {"tool": "merge"}
    many["facts"][150]["extra_field"] = "kept"
    notes = si.render_index_notes(many, max_chars=30_000)
    assert len(notes) > 1
    assert si.parse_index_notes(notes) == many


def test_text_that_looks_like_markup_and_non_ascii_survive():
    index = small_index(3)
    index["facts"][0]["statement"] = 'x < y && "z" > \'w\' </pre><pre> &amp; µm η \U0001d6fc\nnew line\ttab'
    index["header"]["title"] = "T <b>bold</b> & more"
    assert si.parse_index_notes(si.render_index_notes(index)) == index


@pytest.mark.parametrize("kwargs", [
    {},
    {"attrs": True},
    {"br": True},
    {"entities": True},
    {"attrs": True, "br": True, "entities": True},
])
def test_round_trip_after_note_editor_simulation(kwargs):
    index = small_index(3)
    edited = [note_editor(n, **kwargs) for n in si.render_index_notes(index)]
    assert "<pre><code>" in edited[0] and 'data-schema-version="9"' in edited[0]
    assert "<td><p>" in edited[0]
    assert si.parse_index_notes(edited) == index


def test_round_trip_after_note_editor_simulation_many_parts():
    index = big_index(300)
    notes = si.render_index_notes(index, max_chars=40_000)
    assert len(notes) > 1
    edited = [note_editor(n, attrs=True, entities=True) for n in notes]
    assert si.parse_index_notes(edited) == index


def test_round_trip_after_note_editor_simulation_wide_index():
    index = wide_index(300, 60, 3, meaning_len=120)
    notes = si.render_index_notes(index, max_chars=30_000)
    assert len(notes) > 2
    edited = [note_editor(n, attrs=True, br=True, entities=True) for n in notes]
    assert si.parse_index_notes(edited) == index


def test_other_pre_blocks_are_ignored():
    index = small_index(3)
    note = si.render_index_notes(index)[0]
    noisy = "<pre>print('hello')</pre>" + note + "<pre><code>source-index/v0 nothing</code></pre>"
    assert si.parse_index_notes([noisy]) == index


def _raises_invalid(htmls, match):
    with pytest.raises(si.SourceIndexError, match=match) as info:
        si.parse_index_notes(htmls)
    assert info.value.code == "invalid_index"
    assert info.value.problems
    return info.value


def test_parse_rejects_a_missing_part(big):
    _, notes = big
    _raises_invalid(notes[:1] + notes[2:], r"missing part\(s\) 2 of")
    _raises_invalid(notes[:-1], rf"missing part\(s\) {len(notes)} of")
    _raises_invalid(notes[1:], r"missing part\(s\) 1 of")


def test_parse_rejects_mixed_build_ids():
    index = big_index(200)
    other = copy.deepcopy(index)
    other["header"]["build_id"] = "another-build"
    a = si.render_index_notes(index, max_chars=30_000)
    b = si.render_index_notes(other, max_chars=30_000)
    assert len(a) == len(b) > 2
    _raises_invalid([a[0], b[1]] + a[2:], "different builds")


def test_parse_rejects_a_duplicate_part(big):
    _, notes = big
    _raises_invalid(notes + [notes[1]], "more than once")


def test_parse_rejects_disagreeing_part_counts():
    notes = si.render_index_notes(big_index(200), max_chars=30_000)
    n = len(notes)
    lie = notes[1].replace(f"part 2/{n}", f"part 2/{n + 1}")
    _raises_invalid([notes[0], lie] + notes[2:], "disagree on the part count")


def test_parse_rejects_no_block_and_empty_input():
    _raises_invalid(["<p>just a note</p>"], "no source-index block")
    _raises_invalid([], "no source-index block")
    _raises_invalid(["<h1>Source index</h1><p>edited</p><pre>hello</pre>"], "no source-index block")


def test_parse_rejects_broken_json():
    note = si.render_index_notes(small_index(3))[0]
    _raises_invalid([note[: note.rindex("}")] + "</pre>"], "does not parse")
    _raises_invalid([note[: note.rindex("<pre>") + 200]], "does not parse")  # truncated, unclosed pre


def test_parse_rejects_a_later_part_that_holds_a_header_sections_or_unknown_keys():
    notes = si.render_index_notes(big_index(200), max_chars=30_000)
    for tampered in (
        notes[1].replace('"sections":[]', '"sections":[{"id":"S99"}]', 1),
        notes[1].replace('"header":{}', '"header":{"title":"x"}', 1),
        notes[1].replace('{"schema"', '{"x_extra":1,"schema"', 1),
    ):
        assert tampered != notes[1]
        _raises_invalid([notes[0], tampered] + notes[2:], "a later part may hold only")


def test_parse_rejects_an_array_that_is_not_a_list():
    notes = si.render_index_notes(big_index(200), max_chars=30_000)
    bad = notes[1].replace('"vocabulary":[]', '"vocabulary":{}', 1)
    assert bad != notes[1]
    _raises_invalid([notes[0], bad] + notes[2:], "vocabulary is not an array")


def test_parse_reads_a_part_with_only_facts_from_an_older_layout():
    notes = si.render_index_notes(big_index(200), max_chars=30_000)
    facts_only = notes[1].replace(
        '{"schema":"source-index/v1","header":{},"vocabulary":[],"sections":[],"facts":',
        '{"facts":', 1).replace(',"tables_figures":[],"equations":[],"gaps":[]}', "}", 1)
    assert facts_only != notes[1]
    assert si.parse_index_notes([notes[0], facts_only] + notes[2:]) == big_index(200)


def test_parse_rejects_a_block_line_that_disagrees_with_the_header():
    note = si.render_index_notes(small_index(3))[0]
    tampered = note.replace("part 1/1 build sample-20260929-01", "part 1/1 build something-else")
    _raises_invalid([tampered], "does not match")


# ---------------------------------------------------------------------------
# Push and show against a fake Zotero
# ---------------------------------------------------------------------------

class Ctx:
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


class FakeZotero:
    """Child notes in memory, plus the four functions push and show call."""

    def __init__(self):
        self.notes: dict[str, dict] = {}
        self.trashed: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.tag_calls: list[tuple] = []
        self.tag_result = "# Batch Tag Update Results\n\nItems updated: 1"
        self.parents = {PARENT}
        self._n = 0
        self.ctxs: list = []

    def add_note(self, html, parent=PARENT):
        self._n += 1
        key = f"NOTE{self._n:04d}"
        self.notes[key] = {"key": key, "version": self._n, "data": {
            "key": key, "itemType": "note", "parentItem": parent, "note": html,
            "dateAdded": f"2026-09-29T00:00:{self._n:02d}Z",
        }}
        return key

    def html_of(self, key):
        return self.notes[key]["data"]["note"]

    # library backend
    def get_children(self, keys, *, item_type=None):
        self.calls.append(("get_children", tuple(keys), item_type))
        assert item_type == "note"
        return {k: [copy.deepcopy(n) for n in self.notes.values() if n["data"]["parentItem"] == k]
                for k in keys if k in self.parents}

    # note tools
    def create_note(self, item_key, note_title, note_text, tags=None, *, ctx):
        self.calls.append(("create", item_key, note_title))
        self.ctxs.append(ctx)
        assert note_title == "" and "<p>" in note_text   # no extra heading, HTML stored as given
        key = self.add_note(note_text, parent=item_key)
        return f'Successfully created note for "Some Title"\n\nNote key: {key}'

    def update_note(self, item_key, note_text, append=False, *, ctx):
        self.calls.append(("update", item_key))
        self.ctxs.append(ctx)
        assert append is False
        self.notes[item_key]["data"]["note"] = note_text
        return f"Successfully updated note {item_key}"

    def delete_note(self, item_key, *, ctx):
        self.calls.append(("delete", item_key))
        self.ctxs.append(ctx)
        self.trashed[item_key] = self.notes.pop(item_key)
        return f"Successfully trashed note {item_key} (recoverable from Zotero's Trash)"

    def batch_update_tags(self, query="", add_tags=None, remove_tags=None, tag=None, limit=50,
                          item_keys=None, *, ctx):
        self.calls.append(("tags", tuple(item_keys), tuple(add_tags)))
        self.tag_calls.append((list(item_keys), list(add_tags)))
        return self.tag_result

    def writes(self):
        return [c for c in self.calls if c[0] in ("create", "update", "delete", "tags")]


@pytest.fixture
def zotero_reads(monkeypatch):
    """The fake backend and tag tool only: the note tools stay real."""
    from zotero_mcp import library
    from zotero_mcp.tools import write

    fake = FakeZotero()
    monkeypatch.setattr(library, "get_library_backend", lambda: fake)
    monkeypatch.setattr(write, "batch_update_tags", fake.batch_update_tags)
    return fake


@pytest.fixture
def zotero(zotero_reads, monkeypatch):
    from zotero_mcp.tools import annotations

    monkeypatch.setattr(annotations, "create_note", zotero_reads.create_note)
    monkeypatch.setattr(annotations, "update_note", zotero_reads.update_note)
    monkeypatch.setattr(annotations, "delete_note", zotero_reads.delete_note)
    return zotero_reads


@pytest.fixture
def small_parts(monkeypatch):
    """Make push split a 200-fact index into several notes without 2,000 facts."""
    monkeypatch.setattr(si, "DEFAULT_MAX_CHARS", 30_000)
    return big_index(200)


def push(index, **kwargs):
    kwargs.setdefault("replace", False)
    kwargs.setdefault("tags", None)
    kwargs.setdefault("dry_run", False)
    return si.push_index(PARENT, index, ctx=kwargs.pop("ctx", Ctx()), **kwargs)


def test_push_creates_the_notes(zotero):
    index = small_index(3)
    ctx = Ctx()
    result = si.push_index(PARENT, index, replace=False, tags=None, dry_run=False, ctx=ctx)

    assert set(result) == {"item_key", "note_keys", "parts", "chars", "created", "updated", "trashed",
                           "dry_run", "counts"}
    assert result["item_key"] == PARENT
    assert result["note_keys"] == list(zotero.notes) and len(result["note_keys"]) == 1
    assert (result["parts"], result["created"], result["updated"], result["trashed"]) == (1, 1, 0, 0)
    assert result["dry_run"] is False
    assert result["chars"] == sum(len(n) for n in si.render_index_notes(index))
    assert result["counts"] == {"vocabulary": 3, "sections": 3, "facts": 3, "tables_figures": 2,
                                "equations": 1, "gaps": 1}
    assert zotero.ctxs and all(c is ctx for c in zotero.ctxs)
    assert zotero.html_of(result["note_keys"][0]).startswith("<h1>Source index</h1>")
    assert zotero.tag_calls == []


def test_push_creates_every_part_in_order(zotero, small_parts):
    result = push(small_parts)
    assert result["parts"] == result["created"] == len(zotero.notes) > 2
    assert result["note_keys"] == list(zotero.notes)
    assert [c[0] for c in zotero.writes()] == ["create"] * result["parts"]
    assert si.parse_index_notes([zotero.html_of(k) for k in result["note_keys"]]) == small_parts


def test_push_then_show_round_trips(zotero):
    index = sample_index()
    pushed = push(index)
    shown = si.show_index(PARENT, ctx=Ctx())
    assert shown == {"item_key": PARENT, "note_keys": pushed["note_keys"], "parts": 1, "index": index}


def test_push_without_replace_fails_when_an_index_exists(zotero):
    push(small_index(3))
    before = copy.deepcopy(zotero.notes)
    zotero.calls.clear()
    with pytest.raises(si.SourceIndexError) as info:
        push(small_index(4))
    assert info.value.code == "index_exists"
    assert list(before)[0] in str(info.value)
    assert zotero.writes() == [] and zotero.notes == before


def test_push_replace_with_more_parts_updates_in_place_and_adds_parts(zotero, small_parts):
    first = push(small_index(3))
    zotero.calls.clear()
    result = push(small_parts, replace=True)

    n = result["parts"]
    assert n > 2
    assert (result["created"], result["updated"], result["trashed"]) == (n - 1, 1, 0)
    assert result["note_keys"][0] == first["note_keys"][0]      # part 1 kept its note
    assert [c[0] for c in zotero.writes()] == ["update"] + ["create"] * (n - 1)
    assert zotero.trashed == {}
    assert si.show_index(PARENT, ctx=Ctx())["index"] == small_parts


def test_push_replace_with_fewer_parts_trashes_the_surplus(zotero, small_parts):
    first = push(small_parts)
    n = first["parts"]
    zotero.calls.clear()
    small = small_index(3)
    result = push(small, replace=True)

    assert (result["parts"], result["created"], result["updated"], result["trashed"]) == (1, 0, 1, n - 1)
    assert result["note_keys"] == first["note_keys"][:1]
    assert list(zotero.trashed) == first["note_keys"][1:]
    assert list(zotero.notes) == first["note_keys"][:1]
    assert [c[0] for c in zotero.writes()] == ["update"] + ["delete"] * (n - 1)
    shown = si.show_index(PARENT, ctx=Ctx())
    assert shown["index"] == small and shown["parts"] == 1


def test_push_replace_with_the_same_number_of_parts(zotero):
    first = push(small_index(3))
    result = push(sample_index(), replace=True)
    assert (result["created"], result["updated"], result["trashed"]) == (0, 1, 0)
    assert result["note_keys"] == first["note_keys"]
    assert si.show_index(PARENT, ctx=Ctx())["index"] == sample_index()


def test_push_replace_on_an_item_without_an_index_just_creates(zotero):
    result = push(small_index(3), replace=True)
    assert (result["created"], result["updated"], result["trashed"]) == (1, 0, 0)


def test_push_leaves_other_notes_alone(zotero):
    mine = zotero.add_note("<h1>My reading notes</h1><p>keep me</p>")
    plain = zotero.add_note("<p>no heading</p>")
    first = push(small_index(3))
    push(sample_index(), replace=True)
    assert zotero.html_of(mine) == "<h1>My reading notes</h1><p>keep me</p>"
    assert zotero.html_of(plain) == "<p>no heading</p>"
    assert si.show_index(PARENT, ctx=Ctx())["note_keys"] == first["note_keys"]


def test_dry_run_writes_nothing_and_reports_the_plan(zotero, small_parts):
    result = push(small_parts, dry_run=True, tags=["status/indexed"])
    n = len(si.render_index_notes(small_parts, max_chars=30_000))
    assert result["dry_run"] is True
    assert (result["parts"], result["created"], result["updated"], result["trashed"]) == (n, n, 0, 0)
    assert result["note_keys"] == []
    assert result["counts"]["facts"] == 200
    assert result["chars"] == sum(len(h) for h in si.render_index_notes(small_parts, max_chars=30_000))
    assert zotero.writes() == [] and zotero.notes == {} and zotero.tag_calls == []


def test_dry_run_with_replace_plans_against_the_existing_notes(zotero, small_parts):
    first = push(small_parts)
    n = first["parts"]
    before = copy.deepcopy(zotero.notes)
    zotero.calls.clear()
    result = push(small_index(3), replace=True, dry_run=True)
    assert (result["parts"], result["created"], result["updated"], result["trashed"]) == (1, 0, 1, n - 1)
    assert result["note_keys"] == first["note_keys"][:1]
    assert zotero.writes() == [] and zotero.notes == before and zotero.trashed == {}


def test_dry_run_still_reports_an_existing_index(zotero):
    push(small_index(3))
    with pytest.raises(si.SourceIndexError) as info:
        push(small_index(3), dry_run=True)
    assert info.value.code == "index_exists"


def test_push_rejects_an_invalid_index_before_any_write(zotero):
    index = small_index(3)
    index["facts"][0]["section_id"] = "S99"
    index["facts"][1]["page"] = 500
    with pytest.raises(si.SourceIndexError) as info:
        push(index)
    assert info.value.code == "invalid_index"
    assert len(info.value.problems) == 2
    assert zotero.calls == []      # not even a read


def test_push_reports_an_index_too_big_for_a_part(zotero, monkeypatch):
    monkeypatch.setattr(si, "DEFAULT_MAX_CHARS", 500)
    with pytest.raises(si.SourceIndexError) as info:
        push(sample_index())
    assert info.value.code == "invalid_index"
    assert zotero.writes() == []


def test_push_applies_tags_to_the_parent_item(zotero):
    push(small_index(3), tags=["status/indexed", "topic/cooling"])
    assert zotero.tag_calls == [([PARENT], ["status/indexed", "topic/cooling"])]
    push(small_index(3), replace=True, tags="status/indexed, ,topic/cooling")
    assert zotero.tag_calls[-1] == ([PARENT], ["status/indexed", "topic/cooling"])
    zotero.tag_calls.clear()
    push(small_index(3), replace=True, tags=[])
    assert zotero.tag_calls == []


def test_push_reports_a_tag_failure_after_the_notes_are_written(zotero):
    zotero.tag_result = "Error: no writable library"
    with pytest.raises(si.SourceIndexError, match="tagging failed") as info:
        push(small_index(3), tags=["status/indexed"])
    assert info.value.code == "error"
    assert len(zotero.notes) == 1


def test_push_reports_a_failed_note_write(zotero, monkeypatch):
    monkeypatch.setattr(zotero, "create_note", lambda *a, **k: "Error creating note: boom")
    from zotero_mcp.tools import annotations
    monkeypatch.setattr(annotations, "create_note", zotero.create_note)
    with pytest.raises(si.SourceIndexError, match="creating part 1 failed"):
        push(small_index(3))


def test_push_to_an_unknown_item_fails_cleanly(zotero):
    with pytest.raises(si.SourceIndexError, match="NOSUCH01"):
        si.push_index("NOSUCH01", small_index(3), replace=False, tags=None, dry_run=False, ctx=Ctx())
    assert zotero.writes() == []


def test_show_without_an_index(zotero):
    zotero.add_note("<h1>Something else</h1><p>x</p>")
    with pytest.raises(si.SourceIndexError) as info:
        si.show_index(PARENT, ctx=Ctx())
    assert info.value.code == "no_index"


def test_show_with_a_missing_part_is_invalid(zotero, small_parts):
    pushed = push(small_parts)
    zotero.notes.pop(pushed["note_keys"][1])
    with pytest.raises(si.SourceIndexError, match="missing part") as info:
        si.show_index(PARENT, ctx=Ctx())
    assert info.value.code == "invalid_index"


def test_show_sorts_parts_whatever_order_the_notes_come_back(zotero, small_parts):
    pushed = push(small_parts)
    zotero.notes = dict(reversed(list(zotero.notes.items())))
    shown = si.show_index(PARENT, ctx=Ctx())
    assert shown["note_keys"] == pushed["note_keys"]
    assert shown["parts"] == pushed["parts"]
    assert shown["index"] == small_parts


def test_show_reads_notes_the_editor_rewrote(zotero):
    index = sample_index()
    for html in si.render_index_notes(index):
        zotero.add_note(note_editor(html, attrs=True, br=True, entities=True))
    assert si.show_index(PARENT, ctx=Ctx())["index"] == index


def test_show_ignores_trashed_notes_still_listed(zotero):
    push(small_index(3))
    stale = zotero.add_note(zotero.html_of(next(iter(zotero.notes))).replace("part 1/1", "part 1/2"))
    zotero.notes[stale]["data"]["deleted"] = 1
    assert si.show_index(PARENT, ctx=Ctx())["index"] == small_index(3)


def test_show_with_a_section_filters_to_that_section(zotero):
    index = sample_index()
    push(index)
    shown = si.show_index(PARENT, section="S03", ctx=Ctx())
    filtered = shown["index"]

    assert [s["id"] for s in filtered["sections"]] == ["S03"]
    assert [f["id"] for f in filtered["facts"]] == ["F0004", "F0005"]
    assert [t["id"] for t in filtered["tables_figures"]] == ["T01", "T02"]
    assert filtered["equations"] == []                       # E01 sits in S02
    assert [g["id"] for g in filtered["gaps"]] == ["G01"]
    assert filtered["header"] == index["header"]
    assert filtered["vocabulary"] == index["vocabulary"]
    assert filtered["schema"] == index["schema"]
    assert shown["parts"] == 1 and shown["item_key"] == PARENT

    s02 = si.show_index(PARENT, section="S02", ctx=Ctx())["index"]
    assert [f["id"] for f in s02["facts"]] == ["F0003", "F0006"]
    assert [e["id"] for e in s02["equations"]] == ["E01"]
    assert s02["tables_figures"] == [] and s02["gaps"] == []


def test_show_section_filter_keeps_unknown_keys_and_drops_unsectioned_gaps(zotero):
    index = sample_index()
    index["x_provenance"] = {"tool": "merge"}
    index["gaps"].append({"id": "G02", "page": 3, "section_id": None, "kind": "other", "note": "loose"})
    push(index)
    filtered = si.show_index(PARENT, section="S03", ctx=Ctx())["index"]
    assert filtered["x_provenance"] == {"tool": "merge"}
    assert [g["id"] for g in filtered["gaps"]] == ["G01"]


def test_show_section_filter_over_many_parts(zotero, small_parts):
    push(small_parts)
    filtered = si.show_index(PARENT, section="S02", ctx=Ctx())["index"]
    expected = [f for f in small_parts["facts"] if f["section_id"] == "S02"]
    assert filtered["facts"] == expected and expected


def test_show_with_an_unknown_section(zotero):
    push(sample_index())
    with pytest.raises(si.SourceIndexError, match="S99") as info:
        si.show_index(PARENT, section="S99", ctx=Ctx())
    assert info.value.code == "bad_section"
    assert "S01, S02, S03" in str(info.value)


def test_push_through_the_real_note_tools_stores_the_html_unchanged(zotero_reads, monkeypatch):
    """create_note and update_note themselves, with only the Zotero client mocked."""
    from unittest.mock import MagicMock

    from zotero_mcp.tools import annotations

    read_zot = MagicMock()
    read_zot.item.return_value = {"data": {"title": "Parent"}}
    write_zot = MagicMock()
    write_zot.create_items.return_value = {"success": {"0": "REALKEY1"}}
    stored = {"key": "OLDNOTE1", "version": 3, "data": {"itemType": "note", "note": "<p>old</p>"}}
    write_zot.item.return_value = stored
    write_zot.update_item.return_value = MagicMock(status_code=204)
    monkeypatch.setattr(annotations._client, "get_zotero_client", lambda: read_zot)
    monkeypatch.setattr(annotations, "_get_note_write_client", lambda op: (write_zot, None))

    index = small_index(3)
    result = push(index)
    [(payload,)] = write_zot.create_items.call_args.args
    assert payload["itemType"] == "note" and payload["parentItem"] == PARENT
    assert payload["note"] == si.render_index_notes(index)[0]       # no heading added, no re-wrapping
    assert result["note_keys"] == ["REALKEY1"]

    # replace against an existing note goes through update_note
    zotero_reads.add_note(si.render_index_notes(index)[0])
    result = push(sample_index(), replace=True)
    assert stored["data"]["note"] == si.render_index_notes(sample_index())[0]
    assert result["updated"] == 1
