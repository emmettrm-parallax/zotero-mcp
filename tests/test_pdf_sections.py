"""Section maps of synthetic PDFs: outline units, clipping, splitting, chunks, inventory."""

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
            ("S02", "Intro", 1, 2, 2, "outline"),
            ("S03", "Intro > Background", 2, 3, 4, "outline"),
            ("S04", "Intro > Aim", 2, 5, 6, "outline"),
            ("S05", "Method", 1, 7, 7, "outline"),
            ("S06", "Method > Rig", 2, 8, 10, "outline"),
        ]
        assert result["sections"][2]["title"] == "Background"

    def test_level_one_filter_drops_the_subsections(self, tmp_path):
        path = make_pdf(tmp_path, 10, NESTED_TOC)

        result = pdf_sections.sections_for_pdf(path, max_level=1)

        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 1),
            ("Intro", 2, 6),
            ("Method", 7, 10),
        ]

    def test_no_front_unit_when_the_first_entry_is_on_page_one(self, tmp_path):
        path = make_pdf(tmp_path, 4, [[1, "One", 1], [1, "Two", 3]])

        result = pdf_sections.sections_for_pdf(path)

        assert spans(result) == [(1, 2, "outline"), (3, 4, "outline")]
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
            ("C", 2, 4),
            ("D", 5, 8),
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
            ("Early", 2, 5),
            ("Late", 6, 9),
        ]

    def test_rows_without_a_page_are_dropped_but_still_name_their_children(self, monkeypatch, tmp_path):
        toc = [[1, "Part", -1], [2, "Child", 3], [1, "Ghost", 0], [1, "Beyond", 99], [1, "Last", 5]]

        result = self.rows(monkeypatch, tmp_path, 8, toc)

        assert [(s["path"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("Front matter", 1, 2),
            ("Part > Child", 3, 4),
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
            ("Intro > Background", 4, 4),
            ("Intro > Aim", 5, 6),
            ("Method", 7, 7),
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
            ("Method", 7, 7),
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
            ("S01", 1, 1, "outline"),
            ("S02", 2, 5, "split"),
            ("S03", 6, 10, "split"),
            ("S04", 11, 12, "outline"),
        ]

    def test_the_split_is_sized_on_the_clipped_unit(self, tmp_path):
        """Unclipped, "Long" is 19 pages (three parts at a limit of 8); clipped it is 10 (two)."""
        path = make_pdf(tmp_path, 25, [[1, "Long", 1], [1, "Next", 20]])

        result = pdf_sections.sections_for_pdf(path, pages=(10, 25), chunk_pages=8)

        assert [(s["title"], s["start_page"], s["end_page"], s["kind"]) for s in result["sections"]] == [
            ("Long (part 1/2)", 10, 14, "split"),
            ("Long (part 2/2)", 15, 19, "split"),
            ("Next", 20, 25, "outline"),
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

    def test_an_outline_with_no_entry_in_the_scope_gives_chunks(self, tmp_path):
        path = make_pdf(tmp_path, 20, [[1, "Early", 2], [1, "Late", 18]])

        result = pdf_sections.sections_for_pdf(path, pages=(5, 12), chunk_pages=4)

        assert result["source"] == "chunks"
        assert spans(result) == [(5, 8, "chunk"), (9, 12, "chunk")]

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


def add_table_page(doc):
    """A page with a ruled 3-row table under a "Table 1" caption line."""
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Table page", fontsize=12)
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


@pytest.fixture
def inventory_pdf(tmp_path):
    """Pages 1 plain, 2 table, 3 equations, 4 figure; one section per page."""
    doc = pymupdf.open()
    plain = doc.new_page(width=612, height=792)
    plain.insert_text((72, 72), "Plain page", fontsize=12)
    add_table_page(doc)
    add_math_page(doc)
    add_figure_page(doc)
    doc.set_toc([[1, "Plain", 1], [1, "Tables", 2], [1, "Maths", 3], [1, "Figures", 4]])
    path = str(tmp_path / "inventory.pdf")
    doc.save(path)
    doc.close()
    return path


class TestInventory:
    def test_lists_table_caption_equation_label_and_unnumbered_count(self, inventory_pdf):
        result = pdf_sections.sections_for_pdf(inventory_pdf, inventory=True)

        assert [s["path"] for s in result["sections"]] == ["Plain", "Tables", "Maths", "Figures"]
        assert result["inventory"] == [
            {"section_id": "S01", "tables": [], "figures": [], "equations": [], "unnumbered_equations": 0},
            {"section_id": "S02", "tables": ["Table 1"], "figures": [], "equations": [],
             "unnumbered_equations": 0},
            {"section_id": "S03", "tables": [], "figures": [], "equations": ["(3)"],
             "unnumbered_equations": 1},
            {"section_id": "S04", "tables": [], "figures": ["Figure 2"], "equations": [],
             "unnumbered_equations": 0},
        ]

    def test_a_section_over_several_pages_pools_their_labels(self, inventory_pdf, tmp_path):
        doc = pymupdf.open(inventory_pdf)
        doc.set_toc([[1, "All", 1]])
        pooled = str(tmp_path / "pooled.pdf")
        doc.save(pooled)
        doc.close()

        result = pdf_sections.sections_for_pdf(pooled, inventory=True)

        assert len(result["sections"]) == 1
        assert result["inventory"] == [{
            "section_id": "S01", "tables": ["Table 1"], "figures": ["Figure 2"],
            "equations": ["(3)"], "unnumbered_equations": 1,
        }]

    def test_a_page_shared_by_two_sections_is_listed_under_both(self, inventory_pdf, tmp_path):
        doc = pymupdf.open(inventory_pdf)
        doc.set_toc([[1, "First", 2], [1, "Second", 2]])
        shared = str(tmp_path / "shared.pdf")
        doc.save(shared)
        doc.close()

        result = pdf_sections.sections_for_pdf(shared, pages=(2, 2), inventory=True)

        assert [(s["id"], s["start_page"], s["end_page"]) for s in result["sections"]] == [
            ("S01", 2, 2), ("S02", 2, 2),
        ]
        assert [row["tables"] for row in result["inventory"]] == [["Table 1"], ["Table 1"]]

    def test_inventory_follows_a_split_and_the_chunks(self, inventory_pdf):
        result = pdf_sections.sections_for_pdf(inventory_pdf, pages=(2, 4), chunk_pages=2, inventory=True)

        # The "Tables" (p2), "Maths" (p3) and "Figures" (p4) entries are one page each.
        assert [row["section_id"] for row in result["inventory"]] == ["S01", "S02", "S03"]
        assert result["inventory"][0]["tables"] == ["Table 1"]
        assert result["inventory"][1]["equations"] == ["(3)"]
        assert result["inventory"][2]["figures"] == ["Figure 2"]

    def test_a_page_that_fails_to_scan_counts_as_empty(self, inventory_pdf, monkeypatch):
        from zotero_mcp import pdf_layout

        def boom(*_args, **_kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(pdf_layout, "detect_page_regions", boom)
        monkeypatch.setattr(pdf_layout, "scan_math", boom)

        result = pdf_sections.sections_for_pdf(inventory_pdf, inventory=True)

        assert all(not row["tables"] and not row["equations"] and row["unnumbered_equations"] == 0
                   for row in result["inventory"])


class TestMarkdown:
    def test_renders_header_sections_and_inventory(self, inventory_pdf):
        data = pdf_sections.sections_for_pdf(inventory_pdf, inventory=True)
        data.update(key="ABCD1234", attachment_key="EFGH5678", title="A Test Paper")

        markdown = pdf_sections.format_sections_markdown(data)

        assert markdown.startswith("# A Test Paper\n")
        assert "Item `ABCD1234` · attachment `EFGH5678` · 4 pages · source: outline · scope: pages 1-4" in markdown
        assert "| S02 | page 2 | 1 | outline | Tables |" in markdown
        assert "## Inventory" in markdown
        assert "| S02 | Table 1 | - | - | 0 |" in markdown
        assert "| S03 | - | - | (3) | 1 |" in markdown
        assert "| S04 | - | Figure 2 | - | 0 |" in markdown

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
