"""Section maps of synthetic PDFs: outline units, clipping, splitting, chunks, inventory, plot counts."""

from __future__ import annotations

import pymupdf
import pytest

from zotero_mcp import pdf_sections


def make_pdf(tmp_path, page_count, toc=None, name="doc.pdf"):
    """A PDF of ``page_count`` text pages with an optional ``set_toc`` outline."""
    doc = pymupdf.open()
    for number in range(1, page_count + 1):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 72), f"Page {number}", fontsize=12)
    if toc:
        doc.set_toc(toc)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def spans(result):
    """(start, end, kind) per section, for compact assertions."""
    return [(s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]]


NESTED_TOC = [
    [1, "Intro", 2],
    [2, "Background", 3],
    [2, "Aim", 5],
    [1, "Method", 7],
    [2, "Rig", 8],
]


class TestOutlineUnits:
    def test_nesting_to_level_two_with_front_unit_and_paths(self, tmp_path):
        path = make_pdf(tmp_path, 10, NESTED_TOC)

        result = pdf_sections.sections_for_pdf(path, max_level=2)

        assert result["source"] == "outline"
        assert result["scope"] == [1, 10]
        assert result["page_count"] == 10
        assert [(s["id"], s["path"], s["level"], s["start_page"], s["end_page"], s["kind"])
                for s in result["sections"]] == [
            ("S01", "Front matter", 0, 1, 1, "front"),
            ("S02", "Intro", 1, 2, 3, "outline"),
            ("S03", "Intro > Background", 2, 3, 5, "outline"),
            ("S04", "Intro > Aim", 2, 5, 7, "outline"),
            ("S05", "Method", 1, 7, 8, "outline"),
            ("S06", "Method > Rig", 2, 8, 10, "outline"),
        ]
        assert result["sections"][2]["title"] == "Background"

    def test_level_one_filter_drops_the_subsections(self, tmp_path):
        path = make_pdf(tmp_path, 10, NESTED_TOC)

        result = pdf_sections.sections_for_pdf(path, max_level=1)

        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 1),
            ("Intro", 2, 7),
            ("Method", 7, 10),
        ]

    def test_no_front_unit_when_the_first_entry_is_on_page_one(self, tmp_path):
        path = make_pdf(tmp_path, 4, [[1, "One", 1], [1, "Two", 3]])

        result = pdf_sections.sections_for_pdf(path)

        assert spans(result) == [(1, 3, "outline"), (3, 4, "outline")]
        assert all(s["kind"] != "front" for s in result["sections"])

    def test_front_unit_covers_every_page_before_the_first_entry(self, tmp_path):
        path = make_pdf(tmp_path, 6, [[1, "Body", 4]])

        result = pdf_sections.sections_for_pdf(path)

        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Front matter", 1, 3, "front"),
            ("Body", 4, 6, "outline"),
        ]

    def test_entries_on_one_page_share_it(self, tmp_path):
        path = make_pdf(tmp_path, 8, [[1, "A", 2], [1, "B", 2], [1, "C", 2], [1, "D", 5]])

        result = pdf_sections.sections_for_pdf(path)

        assert [(s["title"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 1),
            ("A", 2, 2),
            ("B", 2, 2),
            ("C", 2, 5),
            ("D", 5, 8),
        ]

    def test_a_section_shares_its_last_page_with_the_next_one(self, tmp_path):
        """A section that ends part-way down a page shares it, so no text is lost."""
        path = make_pdf(tmp_path, 12, [[1, "One", 3], [1, "Two", 6], [1, "Three", 10]])

        result = pdf_sections.sections_for_pdf(path)

        assert [(s["title"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 2),
            ("One", 3, 6),
            ("Two", 6, 10),
            ("Three", 10, 12),
        ]

    def test_the_last_entry_runs_to_the_end_of_the_scope(self, tmp_path):
        path = make_pdf(tmp_path, 12, [[1, "One", 3], [1, "Last", 6]])

        whole = pdf_sections.sections_for_pdf(path)
        clipped = pdf_sections.sections_for_pdf(path, pages=(4, 9))

        assert whole["sections"][-1]["end_page"] == 12
        assert [(s["title"], s["start_page"], s["end_page"]) for s in clipped["sections"]] == [
            ("One", 4, 6),
            ("Last", 6, 9),
        ]

    def test_ids_are_sequential_in_page_order(self, tmp_path):
        path = make_pdf(tmp_path, 10, NESTED_TOC)

        result = pdf_sections.sections_for_pdf(path)

        assert [s["id"] for s in result["sections"]] == [f"S{n:02d}" for n in range(1, 7)]
        starts = [s["start_page"] for s in result["sections"]]
        assert starts == sorted(starts)

    def test_engine_leaves_the_caller_fields_empty(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 3, [[1, "A", 1]]))

        assert result["key"] is None and result["attachment_key"] is None and result["title"] is None
        assert set(result) == {"key", "attachment_key", "title", "page_count", "source", "scope", "sections"}
        assert "inventory" not in result

    def test_outline_is_read_in_a_child_process_never_get_toc_here(self, tmp_path, monkeypatch):
        """get_toc() segfaults on some PDFs (#372); this process must not call it."""
        path = make_pdf(tmp_path, 6, [[1, "A", 1], [1, "B", 4]])

        def refuse(self, *args, **kwargs):
            pytest.fail("get_toc() was called in the test process")

        monkeypatch.setattr(pymupdf.Document, "get_toc", refuse)

        result = pdf_sections.sections_for_pdf(path)

        assert [s["title"] for s in result["sections"]] == ["A", "B"]

    def test_a_crashed_outline_read_falls_back_to_chunks(self, tmp_path, monkeypatch):
        from zotero_mcp.tools import write

        monkeypatch.setattr(write, "_extract_pdf_toc", lambda _path: write.TocOutcome("crashed", [], "signal 11"))

        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 10, [[1, "A", 2]]), chunk_pages=5)

        assert result["source"] == "chunks"
        assert spans(result) == [(1, 5, "chunk"), (6, 10, "chunk")]


class TestOutlineRows:
    """Row-level rules, fed raw rows instead of a saved outline."""

    def rows(self, monkeypatch, tmp_path, page_count, toc, max_level=2):
        monkeypatch.setattr(pdf_sections, "_read_outline", lambda _path: toc)
        return pdf_sections.sections_for_pdf(make_pdf(tmp_path, page_count), max_level=max_level)

    def test_rows_out_of_page_order_are_sorted_by_page(self, monkeypatch, tmp_path):
        result = self.rows(monkeypatch, tmp_path, 9, [[1, "Late", 6], [1, "Early", 2]])

        assert [(s["title"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 1),
            ("Early", 2, 6),
            ("Late", 6, 9),
        ]

    def test_rows_without_a_page_are_dropped_but_still_name_their_children(self, monkeypatch, tmp_path):
        toc = [[1, "Part", -1], [2, "Child", 3], [1, "Ghost", 0], [1, "Beyond", 99], [1, "Last", 5]]

        result = self.rows(monkeypatch, tmp_path, 8, toc)

        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 2),
            ("Part > Child", 3, 5),
            ("Last", 5, 8),
        ]

    def test_a_skipped_level_still_gets_a_parent_path(self, monkeypatch, tmp_path):
        toc = [[1, "A", 1], [3, "Deep", 3]]

        shallow = self.rows(monkeypatch, tmp_path, 6, toc, max_level=2)
        deep = self.rows(monkeypatch, tmp_path, 6, toc, max_level=3)

        assert [s["path"] for s in shallow["sections"]] == ["A"]
        assert [s["path"] for s in deep["sections"]] == ["A", "A > Deep"]

    def test_titles_are_stripped_and_blank_titles_get_a_name(self, monkeypatch, tmp_path):
        result = self.rows(monkeypatch, tmp_path, 4, [[1, "  Two\n  Words ", 1], [1, "   ", 3]])

        assert [s["title"] for s in result["sections"]] == ["Two Words", "(untitled)"]


class TestClipping:
    def test_units_are_clipped_to_the_page_range_and_outside_units_dropped(self, tmp_path):
        path = make_pdf(tmp_path, 10, NESTED_TOC)

        result = pdf_sections.sections_for_pdf(path, pages=(4, 8))

        assert result["scope"] == [4, 8]
        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Intro > Background", 4, 5),
            ("Intro > Aim", 5, 7),
            ("Method", 7, 8),
            ("Method > Rig", 8, 8),
        ]
        assert [s["id"] for s in result["sections"]] == ["S01", "S02", "S03", "S04"]

    def test_the_front_unit_is_clipped_too(self, tmp_path):
        path = make_pdf(tmp_path, 10, [[1, "Body", 6]])

        result = pdf_sections.sections_for_pdf(path, pages=(3, 8))

        assert [(s["title"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 3, 5),
            ("Body", 6, 8),
        ]

    def test_a_range_past_the_last_page_is_clamped(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 10, NESTED_TOC), pages=(7, 500))

        assert result["scope"] == [7, 10]
        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Intro > Aim", 7, 7),
            ("Method", 7, 8),
            ("Method > Rig", 8, 10),
        ]

    def test_a_page_list_from_the_cli_reads_as_its_first_to_last_hull(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 10, NESTED_TOC), pages=[3, 4, 5, 6])

        assert result["scope"] == [3, 6]

    @pytest.mark.parametrize("pages", [(0, 3), (5, 2), (11, 12), []])
    def test_a_bad_range_is_a_value_error(self, tmp_path, pages):
        with pytest.raises(ValueError):
            pdf_sections.sections_for_pdf(make_pdf(tmp_path, 10, NESTED_TOC), pages=pages)

    def test_bad_limits_are_value_errors(self, tmp_path):
        path = make_pdf(tmp_path, 3)
        with pytest.raises(ValueError):
            pdf_sections.sections_for_pdf(path, max_level=0)
        with pytest.raises(ValueError):
            pdf_sections.sections_for_pdf(path, chunk_pages=0)


class TestSplitting:
    def test_a_long_unit_splits_into_near_equal_parts_larger_last(self, tmp_path):
        path = make_pdf(tmp_path, 9, [[1, "Results", 1]])

        result = pdf_sections.sections_for_pdf(path, chunk_pages=8)

        assert spans(result) == [(1, 4, "split"), (5, 9, "split")]
        assert [s["title"] for s in result["sections"]] == ["Results (part 1/2)", "Results (part 2/2)"]
        assert [s["path"] for s in result["sections"]] == ["Results (part 1/2)", "Results (part 2/2)"]

    def test_three_parts_share_the_remainder_at_the_end(self, tmp_path):
        path = make_pdf(tmp_path, 20, [[1, "Long", 1]])

        result = pdf_sections.sections_for_pdf(path, chunk_pages=8)

        assert spans(result) == [(1, 6, "split"), (7, 13, "split"), (14, 20, "split")]

    def test_a_unit_at_the_limit_is_not_split(self, tmp_path):
        path = make_pdf(tmp_path, 8, [[1, "Exact", 1]])

        assert spans(pdf_sections.sections_for_pdf(path, chunk_pages=8)) == [(1, 8, "outline")]

    def test_ids_stay_sequential_across_a_split(self, tmp_path):
        path = make_pdf(tmp_path, 12, [[1, "Short", 1], [1, "Long", 2], [1, "End", 11]])

        result = pdf_sections.sections_for_pdf(path, chunk_pages=5)

        assert [(s["id"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("S01", 1, 2, "outline"),
            ("S02", 2, 6, "split"),
            ("S03", 7, 11, "split"),
            ("S04", 11, 12, "outline"),
        ]

    def test_the_split_is_sized_on_the_clipped_unit(self, tmp_path):
        """Unclipped, "Long" is 20 pages (three parts at a limit of 8); clipped it is 11 (two)."""
        path = make_pdf(tmp_path, 25, [[1, "Long", 1], [1, "Next", 20]])

        result = pdf_sections.sections_for_pdf(path, pages=(10, 25), chunk_pages=8)

        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Long (part 1/2)", 10, 14, "split"),
            ("Long (part 2/2)", 15, 20, "split"),
            ("Next", 20, 25, "outline"),
        ]

    def test_a_section_running_to_the_next_start_page_splits_on_that_length(self, tmp_path):
        """Nine pages inclusive (6-14) is two parts, 6-9 and 10-14; eight would not split."""
        path = make_pdf(tmp_path, 15, [[1, "Intro", 1], [1, "Results", 6], [1, "Conclusions", 14]])

        result = pdf_sections.sections_for_pdf(path, max_level=1)

        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Intro", 1, 6, "outline"),
            ("Results (part 1/2)", 6, 9, "split"),
            ("Results (part 2/2)", 10, 14, "split"),
            ("Conclusions", 14, 15, "outline"),
        ]

    def test_the_front_unit_splits_like_any_other(self, tmp_path):
        path = make_pdf(tmp_path, 15, [[1, "Body", 12]])

        result = pdf_sections.sections_for_pdf(path, chunk_pages=6)

        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Front matter (part 1/2)", 1, 5, "split"),
            ("Front matter (part 2/2)", 6, 11, "split"),
            ("Body", 12, 15, "outline"),
        ]


class TestChunks:
    def test_no_outline_gives_fixed_size_chunks(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 20), chunk_pages=8)

        assert result["source"] == "chunks"
        assert result["scope"] == [1, 20]
        assert [(s["id"], s["title"], s["level"], s["start_page"], s["end_page"], s["kind"])
                for s in result["sections"]] == [
            ("S01", "Pages 1-8", 0, 1, 8, "chunk"),
            ("S02", "Pages 9-16", 0, 9, 16, "chunk"),
            ("S03", "Pages 17-20", 0, 17, 20, "chunk"),
        ]

    def test_the_default_chunk_size_is_eight_pages(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 62))

        assert len(result["sections"]) == 8
        assert spans(result)[-1] == (57, 62, "chunk")

    def test_a_single_page_chunk_is_named_by_its_page(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 9), chunk_pages=8)

        assert [s["title"] for s in result["sections"]] == ["Pages 1-8", "Page 9"]

    def test_chunks_cover_only_the_page_range(self, tmp_path):
        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 20), pages=(5, 12), chunk_pages=5)

        assert result["scope"] == [5, 12]
        assert spans(result) == [(5, 9, "chunk"), (10, 12, "chunk")]

    def test_a_scope_inside_one_section_is_that_section_clipped(self, tmp_path):
        """No entry starts in pages 5-12, but "Early" (2-18) covers them: not chunks."""
        path = make_pdf(tmp_path, 20, [[1, "Early", 2], [1, "Late", 18]])

        result = pdf_sections.sections_for_pdf(path, pages=(5, 12), chunk_pages=4)

        assert result["source"] == "outline"
        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Early (part 1/2)", 5, 8, "split"),
            ("Early (part 2/2)", 9, 12, "split"),
        ]

    def test_a_scope_inside_one_short_section_is_a_single_outline_unit(self, tmp_path):
        path = make_pdf(tmp_path, 20, [[1, "Early", 2], [1, "Late", 18]])

        result = pdf_sections.sections_for_pdf(path, pages=(5, 7))

        assert result["source"] == "outline"
        assert spans(result) == [(5, 7, "outline")]
        assert result["sections"][0]["title"] == "Early"

    def test_entries_deeper_than_max_level_alone_give_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pdf_sections, "_read_outline", lambda _path: [[2, "Sub", 3]])

        result = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 6), max_level=1)

        assert result["source"] == "chunks"


def add_math_page(doc):
    """A page with one numbered display equation "(3)" and one unnumbered one."""
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Equation page", fontsize=12)
    page.insert_text((150, 300), "p = rho R T + 2 x", fontsize=12, fontname="cour")
    page.insert_text((500, 300), "(3)", fontsize=11, fontname="helv")
    page.insert_text((150, 400), "a = b + c d e f", fontsize=12, fontname="cour")
    for font in page.get_fonts():
        if font[4] == "cour":  # rename Courier so the page reads as set in a TeX math font
            doc.xref_set_key(font[0], "BaseFont", "/ABCDEF+CMMI10")
    return page


def add_table_page(doc, caption=True):
    """A page with a ruled 3-row table, under a "Table 1" caption line unless ``caption`` is off."""
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Table page", fontsize=12)
    if caption:
        page.insert_text((72, 150), "Table 1: Measured pressure ratios.", fontsize=10)
    for row in range(4):
        y = 170 + row * 20
        page.draw_line((72, y), (400, y), width=0.8)
    for row in range(3):
        page.insert_text((80, 170 + row * 20 + 14), f"case {row}   1.{row}   2.{row}", fontsize=10)
    for x in (72, 180, 290, 400):
        page.draw_line((x, 170), (x, 230), width=0.8)
    return page


def add_figure_page(doc):
    """A page with a raster image above a "Figure 2" caption line."""
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Figure page", fontsize=12)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 140))
    pixmap.clear_with(180)
    page.insert_image(pymupdf.Rect(72, 100, 312, 240), pixmap=pixmap)
    page.insert_text((72, 260), "Figure 2: Test rig layout.", fontsize=10)
    return page


def add_drawings_page(doc):
    """A page with two vector plot frames far apart and no caption."""
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Plot page", fontsize=12)
    for top in (100, 400):
        page.draw_rect(pymupdf.Rect(100, top, 400, top + 200), width=1)
        page.draw_line((100, top + 100), (400, top + 100), width=1)
        page.draw_line((250, top), (250, top + 200), width=1)
    return page


def add_scan_page(doc, text="FIGURE 3. TEST CAPTION", coverage=1.0):
    """A page of one image, ``coverage`` of the page tall, with a text layer line below it."""
    page = doc.new_page(width=612, height=792)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 306, 396))
    pixmap.clear_with(240)
    page.insert_image(pymupdf.Rect(0, 0, 612, 792 * coverage), pixmap=pixmap, keep_proportion=False)
    if text:
        page.insert_text((72, 770), text, fontsize=10)
    return page


def add_text_page(doc, *lines):
    """A page of text lines, 100 points apart, with no graphic."""
    page = doc.new_page(width=612, height=792)
    for number, line in enumerate(lines):
        page.insert_text((72, 100 + 100 * number), line, fontsize=10)
    return page


def add_line_caption_page(doc):
    """Pages set the way Boyce's handbook is: caption lines with no colon or period.

    "Figure 13-19 Title" has no separator, so the layout detector's caption
    pattern skips it. The page also has an in-text line that starts the same way.
    """
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Caption page", fontsize=12)
    page.insert_text((72, 150), "Figure 13-19 Thrust-bearing temperature characteristics.", fontsize=10)
    page.insert_text((72, 200), "Figure 13-20 shows the typical power consumption.", fontsize=10)
    page.insert_text((72, 250), "Table 13-4 Seal materials.", fontsize=10)
    page.insert_text((72, 300), "Fig. 3.1 A dotted number.", fontsize=10)
    page.insert_text((72, 350), "Equation 7 gives the leakage.", fontsize=10)
    page.insert_text((72, 400), "See Figure 9 in the text, not a line start.", fontsize=10)
    return page


def with_outline(path, toc, tmp_path, name):
    """Copy of ``path`` carrying ``toc`` as its outline."""
    doc = pymupdf.open(path)
    doc.set_toc(toc)
    out = str(tmp_path / name)
    doc.save(out)
    doc.close()
    return out


@pytest.fixture
def inventory_pdf(tmp_path):
    """Pages 1 plain, 2 table, 3 equations, 4 figure. No outline."""
    doc = pymupdf.open()
    plain = doc.new_page(width=612, height=792)
    plain.insert_text((72, 72), "Plain page", fontsize=12)
    add_table_page(doc)
    add_math_page(doc)
    add_figure_page(doc)
    path = str(tmp_path / "inventory.pdf")
    doc.save(path)
    doc.close()
    return path


# The figure page's image, as the layout detector boxes it, in the format of ``--rect``.
FIGURE_RECT = "0.1176,0.1263,0.3922,0.1768"
FOUR_CELLS = [[0.10, 0.10, 0.20, 0.10], [0.40, 0.10, 0.20, 0.10],
              [0.10, 0.30, 0.20, 0.10], [0.40, 0.30, 0.20, 0.10]]


def plot_entry(page, plots=0, source="layout", scanned=False, panel_rects=()):
    return {"page": page, "plots": plots, "source": source, "scanned": scanned,
            "panel_rects": list(panel_rects)}


class FakeSplit:
    """Stands in for ``pdf_layout.split_panels``: gives ``cells`` and records each call."""

    def __init__(self, cells):
        self.cells = cells
        self.calls = []

    def __call__(self, page, bbox, **kwargs):
        self.calls.append((list(bbox), kwargs))
        return [list(cell) for cell in self.cells]


@pytest.fixture
def no_split(monkeypatch):
    """A grid split that finds nothing, so a test never depends on the real one."""
    from zotero_mcp import pdf_layout

    fake = FakeSplit([])
    monkeypatch.setattr(pdf_layout, "split_panels", fake, raising=False)
    return fake


@pytest.mark.usefixtures("no_split")
class TestInventory:
    def test_lists_table_caption_equation_label_and_unnumbered_count(self, inventory_pdf):
        """One page per unit (page chunks), so each row holds one page's labels."""
        result = pdf_sections.sections_for_pdf(inventory_pdf, chunk_pages=1, inventory=True)

        assert result["source"] == "chunks"
        assert result["inventory"] == [
            {"section_id": "S01", "tables": [], "figures": [], "equations": [], "unnumbered_equations": 0,
             "plots_on_page": [plot_entry(1)]},
            {"section_id": "S02", "tables": ["Table 1"], "figures": [], "equations": [],
             "unnumbered_equations": 0, "plots_on_page": [plot_entry(2)]},
            {"section_id": "S03", "tables": [], "figures": [], "equations": ["(3)"],
             "unnumbered_equations": 1, "plots_on_page": [plot_entry(3)]},
            {"section_id": "S04", "tables": [], "figures": ["Figure 2"], "equations": [],
             "unnumbered_equations": 0, "plots_on_page": [plot_entry(4, 1, panel_rects=[FIGURE_RECT])]},
        ]

    def test_a_section_over_several_pages_pools_their_labels(self, inventory_pdf, tmp_path):
        pooled = with_outline(inventory_pdf, [[1, "All", 1]], tmp_path, "pooled.pdf")

        result = pdf_sections.sections_for_pdf(pooled, inventory=True)

        assert len(result["sections"]) == 1
        assert result["inventory"] == [{
            "section_id": "S01", "tables": ["Table 1"], "figures": ["Figure 2"],
            "equations": ["(3)"], "unnumbered_equations": 1,
            "plots_on_page": [plot_entry(1), plot_entry(2), plot_entry(3),
                              plot_entry(4, 1, panel_rects=[FIGURE_RECT])],
        }]

    def test_a_page_shared_by_two_sections_is_listed_under_both(self, inventory_pdf, tmp_path):
        shared = with_outline(inventory_pdf, [[1, "First", 2], [1, "Second", 2]], tmp_path, "shared.pdf")

        result = pdf_sections.sections_for_pdf(shared, pages=(2, 2), inventory=True)

        assert [(s["id"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("S01", 2, 2), ("S02", 2, 2),
        ]
        assert [row["tables"] for row in result["inventory"]] == [["Table 1"], ["Table 1"]]

    def test_the_next_sections_first_page_is_scanned_for_the_section_before(self, inventory_pdf, tmp_path):
        """Under the shared-page rule "Plain" spans pages 1-2, so it lists page 2's table."""
        path = with_outline(inventory_pdf, [[1, "Plain", 1], [1, "Tables", 2]], tmp_path, "adjacent.pdf")

        result = pdf_sections.sections_for_pdf(path, inventory=True)

        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Plain", 1, 2), ("Tables", 2, 4),
        ]
        assert [row["tables"] for row in result["inventory"]] == [["Table 1"], ["Table 1"]]
        assert result["inventory"][1]["equations"] == ["(3)"]
        assert result["inventory"][1]["figures"] == ["Figure 2"]

    def test_inventory_follows_a_split(self, inventory_pdf, tmp_path):
        path = with_outline(inventory_pdf, [[1, "All", 1]], tmp_path, "split.pdf")

        result = pdf_sections.sections_for_pdf(path, pages=(2, 4), chunk_pages=2, inventory=True)

        assert [(s["id"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("S01", 2, 2, "split"), ("S02", 3, 4, "split"),
        ]
        assert result["inventory"][0]["tables"] == ["Table 1"]
        assert result["inventory"][1]["equations"] == ["(3)"]
        assert result["inventory"][1]["figures"] == ["Figure 2"]

    def test_inventory_follows_the_chunks(self, inventory_pdf):
        result = pdf_sections.sections_for_pdf(inventory_pdf, pages=(2, 4), chunk_pages=2, inventory=True)

        assert [row["section_id"] for row in result["inventory"]] == ["S01", "S02"]
        assert result["inventory"][0]["tables"] == ["Table 1"]
        assert result["inventory"][0]["equations"] == ["(3)"]
        assert result["inventory"][1]["figures"] == ["Figure 2"]

    def test_a_page_that_fails_to_scan_counts_as_empty(self, inventory_pdf, monkeypatch):
        from zotero_mcp import pdf_layout

        def boom(*_args, **_kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(pdf_layout, "detect_page_regions", boom)
        monkeypatch.setattr(pdf_layout, "scan_math", boom)
        monkeypatch.setattr(pdf_sections, "_line_labels", boom)
        monkeypatch.setattr(pdf_sections, "_caption_labels", boom)

        result = pdf_sections.sections_for_pdf(inventory_pdf, inventory=True)

        assert result["inventory"] == [
            {"section_id": "S01", "tables": [], "figures": [], "equations": [], "unnumbered_equations": 0,
             "plots_on_page": [plot_entry(page, source="labels") for page in (1, 2, 3, 4)]}
        ]

    def test_a_failed_layout_scan_still_lists_the_line_labels(self, inventory_pdf, monkeypatch):
        from zotero_mcp import pdf_layout

        def boom(*_args, **_kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(pdf_layout, "detect_page_regions", boom)
        monkeypatch.setattr(pdf_layout, "scan_math", boom)

        row = pdf_sections.sections_for_pdf(inventory_pdf, inventory=True)["inventory"][0]

        assert row["tables"] == ["Table 1"]
        assert row["figures"] == ["Figure 2"]


def build_pdf(tmp_path, *builders, name="built.pdf"):
    """A PDF whose pages come from the page builders, in order."""
    doc = pymupdf.open()
    for build in builders:
        build(doc)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def only_page(path):
    """The inventory row and plot entry of a one-page PDF."""
    row = pdf_sections.sections_for_pdf(path, inventory=True)["inventory"][0]
    assert len(row["plots_on_page"]) == 1
    return row, row["plots_on_page"][0]


def use_split(monkeypatch, cells):
    from zotero_mcp import pdf_layout

    fake = FakeSplit(cells)
    monkeypatch.setattr(pdf_layout, "split_panels", fake, raising=False)
    return fake


class TestPlotsOnPage:
    """Each inventory row counts the plots of each of its pages."""

    def test_drawing_boxes_count_one_each_and_never_call_the_split(self, tmp_path, monkeypatch):
        fake = use_split(monkeypatch, FOUR_CELLS)

        _row, entry = only_page(build_pdf(tmp_path, add_drawings_page))

        assert entry == plot_entry(1, 2, "layout", panel_rects=[
            "0.1634,0.1263,0.4902,0.2525", "0.1634,0.5051,0.4902,0.2525"])
        assert fake.calls == []

    def test_an_image_box_counts_the_cells_of_the_split(self, tmp_path, monkeypatch):
        fake = use_split(monkeypatch, FOUR_CELLS)

        row, entry = only_page(build_pdf(tmp_path, add_figure_page))

        assert entry["plots"] == 4
        assert entry["source"] == "grid"
        assert entry["panel_rects"] == [
            "0.1000,0.1000,0.2000,0.1000", "0.4000,0.1000,0.2000,0.1000",
            "0.1000,0.3000,0.2000,0.1000", "0.4000,0.3000,0.2000,0.1000",
        ]
        assert len(fake.calls) == 1
        called_box, called_options = fake.calls[0]
        assert [round(value, 4) for value in called_box] == [0.1176, 0.1263, 0.3922, 0.1768]
        assert called_options == {}
        assert row["figures"] == ["Figure 2"]

    @pytest.mark.parametrize("cells", [[], [[0.2, 0.2, 0.3, 0.1]]], ids=["none", "one"])
    def test_an_image_box_that_the_split_cannot_cut_counts_once(self, tmp_path, monkeypatch, cells):
        use_split(monkeypatch, cells)

        _row, entry = only_page(build_pdf(tmp_path, add_figure_page))

        assert entry == plot_entry(1, 1, "layout", panel_rects=[FIGURE_RECT])

    def test_a_missing_split_counts_each_box_as_one(self, tmp_path, monkeypatch):
        from zotero_mcp import pdf_layout

        monkeypatch.delattr(pdf_layout, "split_panels", raising=False)

        _row, entry = only_page(build_pdf(tmp_path, add_figure_page))

        assert entry == plot_entry(1, 1, "layout", panel_rects=[FIGURE_RECT])

    def test_a_split_that_raises_counts_each_box_as_one(self, tmp_path, monkeypatch):
        from zotero_mcp import pdf_layout

        def boom(*_args, **_kwargs):
            raise RuntimeError("split failed")

        monkeypatch.setattr(pdf_layout, "split_panels", boom, raising=False)

        _row, entry = only_page(build_pdf(tmp_path, add_figure_page))

        assert entry == plot_entry(1, 1, "layout", panel_rects=[FIGURE_RECT])

    def test_a_table_box_does_not_count_on_a_page_with_a_table_label(self, tmp_path, no_split):
        row, entry = only_page(build_pdf(tmp_path, add_table_page))

        assert row["tables"] == ["Table 1"]
        assert entry == plot_entry(1)

    def test_a_table_box_counts_once_on_a_page_with_no_table_label(self, tmp_path, no_split):
        row, entry = only_page(build_pdf(tmp_path, lambda doc: add_table_page(doc, caption=False)))

        assert row["tables"] == []
        assert entry["plots"] == 1
        assert entry["source"] == "layout"
        assert len(entry["panel_rects"]) == 1
        assert no_split.calls == []

    def test_an_equation_box_never_counts(self, tmp_path, no_split):
        row, entry = only_page(build_pdf(tmp_path, add_math_page))

        assert row["equations"] == ["(3)"]
        assert entry == plot_entry(1)

    def test_a_scanned_page_with_no_box_is_split_whole_with_its_text_masked(self, tmp_path, monkeypatch):
        fake = use_split(monkeypatch, FOUR_CELLS[:3])

        row, entry = only_page(build_pdf(tmp_path, add_scan_page))

        assert entry["scanned"] is True
        assert entry["plots"] == 3
        assert entry["source"] == "grid"
        assert len(entry["panel_rects"]) == 3
        assert fake.calls == [([0, 0, 1, 1], {"mask_text": True})]
        assert row["figures"] == ["FIGURE 3"]

    def test_a_scanned_page_that_the_split_cannot_cut_falls_back_to_its_captions(self, tmp_path, monkeypatch):
        use_split(monkeypatch, [])

        row, entry = only_page(build_pdf(tmp_path, add_scan_page))

        assert entry == plot_entry(1, 1, "captions", scanned=True)
        assert row["figures"] == ["FIGURE 3"]

    def test_a_scanned_page_with_no_caption_and_no_cell_counts_zero(self, tmp_path, monkeypatch):
        use_split(monkeypatch, [])

        _row, entry = only_page(build_pdf(tmp_path, lambda doc: add_scan_page(doc, text="")))

        assert entry == plot_entry(1, 0, "grid", scanned=True)

    @pytest.mark.parametrize("coverage, scanned", [(0.9, False), (0.96, True)])
    def test_a_scan_is_one_image_over_95_percent_of_the_page(self, tmp_path, no_split, coverage, scanned):
        _row, entry = only_page(build_pdf(tmp_path, lambda doc: add_scan_page(doc, coverage=coverage)))

        assert entry["scanned"] is scanned

    def test_a_layout_exception_counts_the_figure_labels(self, tmp_path, monkeypatch, no_split):
        from zotero_mcp import pdf_layout

        def boom(*_args, **_kwargs):
            raise RuntimeError("layout failed")

        monkeypatch.setattr(pdf_layout, "detect_page_regions", boom)

        row, entry = only_page(build_pdf(tmp_path, add_line_caption_page))

        assert row["figures"] == ["Figure 13-19", "Figure 13-20", "Fig. 3.1"]
        assert entry == plot_entry(1, 3, "labels")
        assert no_split.calls == []

    def test_a_layout_error_counts_the_figure_labels(self, tmp_path, monkeypatch, no_split):
        from zotero_mcp import pdf_layout

        monkeypatch.setattr(pdf_layout, "detect_page_regions", lambda *_a, **_k: {"error": "no page"})

        _row, entry = only_page(build_pdf(tmp_path, add_figure_page))

        assert entry == plot_entry(1, 1, "labels")

    def test_an_in_text_mention_is_listed_but_is_not_a_plot(self, tmp_path, no_split):
        row, entry = only_page(build_pdf(tmp_path, lambda doc: add_text_page(doc, "Figure 5 shows the leakage.")))

        assert row["figures"] == ["Figure 5"]
        assert entry == plot_entry(1)

    def test_distinct_caption_blocks_are_the_count_when_there_is_no_graphic(self, tmp_path, no_split):
        def captions(doc):
            add_text_page(doc, "Figure 4. First plot.", "FIGURE 5. Second plot.", "Fig. 4. First plot again.")

        row, entry = only_page(build_pdf(tmp_path, captions))

        assert entry == plot_entry(1, 2, "captions")
        assert row["figures"] == ["Figure 4", "FIGURE 5"]

    def test_a_caption_set_in_capitals_is_listed_as_a_figure_label(self, tmp_path, no_split):
        row, _entry = only_page(build_pdf(tmp_path, lambda doc: add_text_page(doc, "FIGURE 10. TITLE")))

        assert row["figures"] == ["FIGURE 10"]

    def test_a_shared_page_is_listed_in_both_rows_with_its_own_copy(self, inventory_pdf, tmp_path, no_split):
        shared = with_outline(inventory_pdf, [[1, "First", 4], [1, "Second", 4]], tmp_path, "shared-plots.pdf")

        rows = pdf_sections.sections_for_pdf(shared, pages=(4, 4), inventory=True)["inventory"]

        expected = [plot_entry(4, 1, panel_rects=[FIGURE_RECT])]
        assert [row["plots_on_page"] for row in rows] == [expected, expected]
        rows[0]["plots_on_page"][0]["panel_rects"].append("0,0,1,1")
        assert rows[1]["plots_on_page"][0]["panel_rects"] == [FIGURE_RECT]

    def test_a_section_over_several_pages_lists_one_entry_per_page_in_order(self, tmp_path, no_split):
        path = build_pdf(tmp_path, lambda doc: add_text_page(doc, "Plain."), add_drawings_page, add_figure_page)

        row = pdf_sections.sections_for_pdf(path, inventory=True)["inventory"][0]

        assert [(entry["page"], entry["plots"]) for entry in row["plots_on_page"]] == [(1, 0), (2, 2), (3, 1)]

    def test_the_markdown_plots_column_sums_the_pages_of_a_row(self, tmp_path, no_split):
        path = build_pdf(tmp_path, add_drawings_page, add_figure_page)
        data = pdf_sections.sections_for_pdf(path, inventory=True)

        markdown = pdf_sections.format_sections_markdown(data)

        assert "| S01 | - | Figure 2 | - | 0 | 3 |" in markdown


@pytest.mark.usefixtures("no_split")
class TestLineLabels:
    """Captions with no colon or period ("Figure 13-19 Title") come from the line pattern."""

    @pytest.fixture
    def caption_pdf(self, tmp_path):
        doc = pymupdf.open()
        add_line_caption_page(doc)
        path = str(tmp_path / "captions.pdf")
        doc.save(path)
        doc.close()
        return path

    def test_the_layout_pattern_alone_misses_a_boyce_style_caption(self, caption_pdf):
        """Pins why the second pattern exists: the layout detector skips "Figure 13-19 Title"."""
        from zotero_mcp.pdf_layout import _parse_caption_block

        assert _parse_caption_block("Figure 13-19 Thrust-bearing temperature characteristics.") is None

    def test_a_boyce_style_caption_is_listed(self, caption_pdf):
        result = pdf_sections.sections_for_pdf(caption_pdf, inventory=True)

        row = result["inventory"][0]
        assert "Figure 13-19" in row["figures"]
        assert "Table 13-4" in row["tables"]
        assert "Fig. 3.1" in row["figures"]

    def test_in_text_line_starts_are_included_and_mid_line_mentions_are_not(self, caption_pdf):
        """Over-inclusion is accepted: "Figure 13-20 shows ..." starts a line, so it is listed."""
        row = pdf_sections.sections_for_pdf(caption_pdf, inventory=True)["inventory"][0]

        assert "Figure 13-20" in row["figures"]
        assert "Figure 9" not in row["figures"]

    def test_equation_words_list_under_equations(self, caption_pdf):
        row = pdf_sections.sections_for_pdf(caption_pdf, inventory=True)["inventory"][0]

        assert row["equations"] == ["Equation 7"]

    def test_a_label_the_layout_pass_already_found_is_listed_once(self, inventory_pdf):
        """The pages carry "Table 1: ..." and "Figure 2: ...": both patterns see them."""
        rows = pdf_sections.sections_for_pdf(inventory_pdf, chunk_pages=4, inventory=True)["inventory"]

        assert rows[0]["tables"] == ["Table 1"]
        assert rows[0]["figures"] == ["Figure 2"]

    @pytest.mark.parametrize("left, right, kind", [
        ("Fig. 3", "Figure 3", "figures"),
        ("figure 3", "Figure 3", "figures"),
        ("Eq. 3", "(3)", "equations"),
        ("Equation 3", "Eq. 3", "equations"),
    ])
    def test_spellings_of_one_label_compare_equal(self, left, right, kind):
        assert pdf_sections._label_key(left, kind) == pdf_sections._label_key(right, kind)

    def test_lettered_equation_labels_stay_apart(self):
        target: list[str] = []
        pdf_sections._extend_unique(target, ["(1a)", "(1b)", "(1a)"], "equations")

        assert target == ["(1a)", "(1b)"]

    def test_a_page_that_fails_the_line_scan_still_lists_its_layout_labels(self, inventory_pdf, monkeypatch):
        def boom(_page):
            raise RuntimeError("no text")

        monkeypatch.setattr(pdf_sections, "_line_labels", boom)

        rows = pdf_sections.sections_for_pdf(inventory_pdf, chunk_pages=4, inventory=True)["inventory"]

        assert rows[0]["tables"] == ["Table 1"]
        assert rows[0]["figures"] == ["Figure 2"]


@pytest.mark.usefixtures("no_split")
class TestMarkdown:
    def test_renders_header_sections_and_inventory(self, inventory_pdf, tmp_path):
        path = with_outline(
            inventory_pdf, [[1, "Plain", 1], [1, "Tables", 2], [1, "Maths", 3], [1, "Figures", 4]],
            tmp_path, "markdown.pdf",
        )
        data = pdf_sections.sections_for_pdf(path, inventory=True)
        data.update(key="ABCD1234", attachment_key="EFGH5678", title="A Test Paper")

        markdown = pdf_sections.format_sections_markdown(data)

        assert markdown.startswith("# A Test Paper\n")
        assert "Item `ABCD1234` · attachment `EFGH5678` · 4 pages · source: outline · scope: pages 1-4" in markdown
        assert "| S02 | pages 2-3 | 1 | outline | Tables |" in markdown
        assert "## Inventory" in markdown
        assert "| Section | Tables | Figures | Equations | Unnumbered equations | Plots |" in markdown
        assert "| S01 | Table 1 | - | - | 0 | 0 |" in markdown
        assert "| S02 | Table 1 | - | (3) | 1 | 0 |" in markdown
        assert "| S03 | - | Figure 2 | (3) | 1 | 1 |" in markdown  # page 4 is shared with S04
        assert "| S04 | - | Figure 2 | - | 0 | 1 |" in markdown

    def test_without_inventory_or_caller_fields(self, tmp_path):
        data = pdf_sections.sections_for_pdf(make_pdf(tmp_path, 12), chunk_pages=8)

        markdown = pdf_sections.format_sections_markdown(data)

        assert markdown.startswith("# Sections\n")
        assert "Inventory" not in markdown
        assert "Item `" not in markdown
        assert "12 pages · source: chunks · scope: pages 1-12" in markdown
        assert "| S01 | pages 1-8 | 0 | chunk | Pages 1-8 |" in markdown
        assert "| S02 | pages 9-12 | 0 | chunk | Pages 9-12 |" in markdown

    def test_pipes_in_a_title_do_not_break_the_table(self):
        data = {
            "key": None, "attachment_key": None, "title": None, "page_count": 3, "source": "outline",
            "scope": [1, 3],
            "sections": [{"id": "S01", "title": "a|b", "path": "P > a|b", "level": 2,
                          "start_page": 1, "end_page": 3, "kind": "outline"}],
        }

        assert "| P > a\\|b |" in pdf_sections.format_sections_markdown(data)

    def test_an_empty_section_list_says_so(self):
        data = {"key": None, "attachment_key": None, "title": None, "page_count": 3, "source": "chunks",
                "scope": [2, 2], "sections": []}

        assert "No sections." in pdf_sections.format_sections_markdown(data)
