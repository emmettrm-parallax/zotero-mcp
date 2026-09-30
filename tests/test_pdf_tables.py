"""Table cells from a PDF page (``zotero_mcp.pdf_tables``).

The synthetic PDFs are drawn with PyMuPDF, in the style of the ``prose_pdf``
fixture of ``test_cli_reading_workflow``. Two kinds of table matter:

* a ruled grid, which ``lines`` finds;
* a booktabs table (rules, no vertical lines), which only ``text`` finds, and
  only under a ``Table N`` caption.
"""

from types import SimpleNamespace

import pymupdf
import pytest

from zotero_mcp import pdf_tables

PAGE_W, PAGE_H = 612, 792

ALLOYS = [
    ["Alloy", "Temp (C)", "Modulus (GPa)"],
    ["Inconel 718", "650", "200"],
    ["Haynes 230", "1150", "211"],
]
# One word per cell: the text strategy drops a word that no other word lines up with
# ("(GPa)" above), which is why a reader checks the page image.
PLAIN = [["Alloy", "Temp", "Modulus"], ["Inconel", "650", "200"], ["Haynes", "1150", "211"]]


def _new_pdf(tmp_path, draw, *, pages=1, name="doc.pdf"):
    """A PDF whose page ``n`` (1-based) is drawn by ``draw(page, n)``."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        draw(page, n)
    path = str(tmp_path / name)
    doc.save(path)
    doc.close()
    return path


def _grid(page, data, *, x=72, y=200, col_w=120, row_h=22):
    """A ruled grid: every cell boxed, text set in each non-empty cell. Returns the grid's bottom."""
    rows, cols = len(data), len(data[0])
    for i in range(rows + 1):
        page.draw_line((x, y + i * row_h), (x + cols * col_w, y + i * row_h), width=0.8)
    for j in range(cols + 1):
        page.draw_line((x + j * col_w, y), (x + j * col_w, y + rows * row_h), width=0.8)
    for i, row in enumerate(data):
        for j, text in enumerate(row):
            if text:
                page.insert_text((x + j * col_w + 6, y + i * row_h + 15), text, fontsize=10)
    return y + rows * row_h


def _booktabs(page, data, *, x=72, y=200, col_w=120, row_h=16, width=None):
    """A booktabs table: top, header and bottom rules, no vertical lines. Returns the bottom rule's y."""
    width = width or col_w * len(data[0])
    for i, row in enumerate(data):
        for j, text in enumerate(row):
            page.insert_text((x + j * col_w, y + 12 + i * row_h), text, fontsize=10)
    rules = (y, y + row_h, y + len(data) * row_h)
    for rule_y in rules:
        page.draw_line((x, rule_y), (x + width, rule_y), width=0.8)
    return rules[-1]


def _caption(page, text, y, *, x=72):
    page.insert_text((x, y), text, fontsize=10)


def _tables(path, page=1, strategy="lines"):
    return pdf_tables.tables_for_pdf(path, pages=[page], strategy=strategy)["pages"][0]["tables"]


def _filled(rows):
    """The rows that hold text: the text strategy also returns blank rows between the text lines."""
    return [row for row in rows if any(cell for cell in row)]


class TestLinesStrategy:
    def test_finds_a_ruled_grid(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: _grid(page, ALLOYS))
        data = pdf_tables.tables_for_pdf(path, pages=[1], strategy="lines")
        assert data["strategy"] == "lines"
        assert [p["page"] for p in data["pages"]] == [1]
        (table,) = data["pages"][0]["tables"]
        assert table["id"] == "p1-t1"
        assert table["header"] == ["Alloy", "Temp (C)", "Modulus (GPa)"]
        assert table["rows"] == [["Inconel 718", "650", "200"], ["Haynes 230", "1150", "211"]]
        assert table["caption"] is None
        x, y, w, h = table["bbox"]
        assert (x, y) == pytest.approx((72 / PAGE_W, 200 / PAGE_H), abs=1e-3)
        assert (w, h) == pytest.approx((360 / PAGE_W, 66 / PAGE_H), abs=1e-3)
        assert table["rect_arg"] == f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}"

    def test_bbox_is_rounded_to_four_decimals(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: _grid(page, ALLOYS, x=73.3, y=201.7))
        (table,) = _tables(path)
        assert table["bbox"] == [round(v, 4) for v in table["bbox"]]

    def test_sparse_grid_without_a_caption_is_dropped(self, tmp_path):
        sparse = [["", "", "", ""], ["", "12", "", ""], ["", "", "", ""], ["", "", "", "7"]]
        path = _new_pdf(tmp_path, lambda page, n: _grid(page, sparse, col_w=100))
        assert _tables(path) == []

    def test_sparse_grid_with_a_caption_is_kept(self, tmp_path):
        sparse = [["", "", "", ""], ["", "12", "", ""], ["", "", "", ""], ["", "", "", "7"]]

        def draw(page, n):
            _caption(page, "Table 4. Sparse but captioned", 190)
            _grid(page, sparse, col_w=100)

        (table,) = _tables(_new_pdf(tmp_path, draw))
        assert table["caption"] == "Table 4. Sparse but captioned"

    def test_two_stacked_tables_stay_apart(self, tmp_path):
        first = [["Seal", "Leak"], ["Labyrinth", "1.0"], ["Brush", "0.2"]]
        second = [["Coating", "Wear"], ["AlSi", "5"], ["NiCrAl", "3"]]

        def draw(page, n):
            bottom = _grid(page, first, y=200)
            _grid(page, second, y=bottom + 40)

        one, two = _tables(_new_pdf(tmp_path, draw))
        assert (one["id"], two["id"]) == ("p1-t1", "p1-t2")
        assert one["header"] == ["Seal", "Leak"]
        assert two["header"] == ["Coating", "Wear"]
        assert one["bbox"][1] + one["bbox"][3] < two["bbox"][1]
        assert one["rows"] == [["Labyrinth", "1.0"], ["Brush", "0.2"]]
        assert two["rows"] == [["AlSi", "5"], ["NiCrAl", "3"]]

    def test_stacked_tables_each_take_the_caption_above_them(self, tmp_path):
        first = [["Seal", "Leak"], ["Labyrinth", "1.0"]]
        second = [["Coating", "Wear"], ["AlSi", "5"]]

        def draw(page, n):
            _caption(page, "Table 1. Seals", 190)
            bottom = _grid(page, first, y=200)
            _caption(page, "Table 2. Coatings", bottom + 30)
            _grid(page, second, y=bottom + 40)

        one, two = _tables(_new_pdf(tmp_path, draw))
        assert (one["caption"], two["caption"]) == ("Table 1. Seals", "Table 2. Coatings")

    def test_caption_below_a_table_is_found(self, tmp_path):
        def draw(page, n):
            bottom = _grid(page, ALLOYS)
            _caption(page, "Table 3. Alloy limits at temperature", bottom + 16)

        (table,) = _tables(_new_pdf(tmp_path, draw))
        assert table["caption"] == "Table 3. Alloy limits at temperature"

    def test_caption_beyond_the_layout_distance_is_not_attached(self, tmp_path):
        def draw(page, n):
            bottom = _grid(page, ALLOYS)
            _caption(page, "Table 3. Far away", bottom + 0.15 * PAGE_H + 30)

        (table,) = _tables(_new_pdf(tmp_path, draw))
        assert table["caption"] is None

    def test_caption_in_the_other_column_is_not_attached(self, tmp_path):
        def draw(page, n):
            _grid(page, [["A", "B"], ["1", "2"]], x=72, col_w=60, y=200)
            _caption(page, "Table 9. Right column", 190, x=400)

        (table,) = _tables(_new_pdf(tmp_path, draw))
        assert table["caption"] is None

    def test_a_caption_serves_one_table(self, tmp_path):
        first = [["Seal", "Leak"], ["Labyrinth", "1.0"]]
        second = [["Coating", "Wear"], ["AlSi", "5"]]

        def draw(page, n):
            _caption(page, "Table 1. Seals", 190)
            bottom = _grid(page, first, y=200)
            _grid(page, second, y=bottom + 30)

        one, two = _tables(_new_pdf(tmp_path, draw))
        assert one["caption"] == "Table 1. Seals"
        assert two["caption"] is None

    def test_cell_text_is_normalized(self, tmp_path):
        data = [["Range", "Note"], ["1300-1600", "a  b"]]
        path = _new_pdf(tmp_path, lambda page, n: _grid(page, data))
        (table,) = _tables(path)
        assert table["rows"] == [["1300-1600", "a b"]]


class TestTextStrategy:
    def test_only_text_finds_a_booktabs_table_under_a_caption(self, tmp_path):
        def draw(page, n):
            _caption(page, "Table 1. Alloy limits", 188)
            _booktabs(page, PLAIN)

        path = _new_pdf(tmp_path, draw)
        assert _tables(path, strategy="lines") == []
        data = pdf_tables.tables_for_pdf(path, pages=[1], strategy="text")
        assert data["strategy"] == "text"
        (table,) = data["pages"][0]["tables"]
        assert table["id"] == "p1-t1"
        assert table["caption"] == "Table 1. Alloy limits"
        assert table["header"] == ["Alloy", "Temp", "Modulus"]
        assert _filled(table["rows"]) == [["Inconel", "650", "200"], ["Haynes", "1150", "211"]]
        x, y, w, h = table["bbox"]
        # the box is the rule chain (x 72-432, y 200-248 pt) with a 2 pt margin, to 4 decimals
        assert (x, y) == pytest.approx((70 / PAGE_W, 198 / PAGE_H), abs=1e-3)
        assert (x + w, y + h) == pytest.approx((434 / PAGE_W, 250 / PAGE_H), abs=1e-3)

    def test_the_box_spans_the_rules_when_the_text_is_narrower(self, tmp_path):
        """The text strategy drops words that fit no column, so its own box can miss a column."""
        def draw(page, n):
            _caption(page, "Table 1. Alloy limits", 188)
            _booktabs(page, PLAIN, col_w=60, width=400)  # the words end near x 250; the rules run to x 472

        (table,) = _tables(_new_pdf(tmp_path, draw), strategy="text")
        x, y, w, h = table["bbox"]
        assert x + w >= 472 / PAGE_W - 1e-3
        assert table["rect_arg"] == f"{x:.4f},{y:.4f},{w:.4f},{h:.4f}"

    def test_text_needs_a_caption(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: _booktabs(page, ALLOYS))
        assert _tables(path, strategy="text") == []

    def test_a_caption_with_no_rule_below_it_gives_no_table(self, tmp_path):
        def draw(page, n):
            _caption(page, "Table 1. Alloy limits", 188)
            _booktabs(page, ALLOYS, y=188 + 0.10 * PAGE_H + 20)  # first rule too far below

        assert _tables(_new_pdf(tmp_path, draw), strategy="text") == []

    def test_a_table_under_a_caption_in_the_other_column_is_not_taken(self, tmp_path):
        def draw(page, n):
            _caption(page, "Table 1. Left column", 188, x=72)
            _booktabs(page, [["A", "B"], ["1", "2"]], x=340, col_w=100)

        assert _tables(_new_pdf(tmp_path, draw), strategy="text") == []

    def test_a_chain_stops_at_the_next_caption(self, tmp_path):
        first = [["Seal", "Leak"], ["Labyrinth", "1.0"], ["Brush", "0.2"]]
        second = [["Coating", "Wear"], ["AlSi", "5"], ["NiCrAl", "3"]]

        def draw(page, n):
            _caption(page, "Table 1. Seals", 188)
            bottom = _booktabs(page, first, y=200, col_w=150)
            _caption(page, "Table 2. Coatings", bottom + 30)
            _booktabs(page, second, y=bottom + 42, col_w=150)

        one, two = _tables(_new_pdf(tmp_path, draw), strategy="text")
        assert one["caption"] == "Table 1. Seals"
        assert _filled(one["rows"]) == [["Labyrinth", "1.0"], ["Brush", "0.2"]]
        assert two["caption"] == "Table 2. Coatings"
        assert _filled(two["rows"]) == [["AlSi", "5"], ["NiCrAl", "3"]]
        assert one["bbox"][1] + one["bbox"][3] < two["bbox"][1]

    def test_a_caption_in_the_other_column_does_not_end_the_chain(self, tmp_path):
        rows = [["Seal", "Leak"]] + [[f"Type{i}", str(i)] for i in range(1, 8)]

        def draw(page, n):
            _caption(page, "Table 1. Left column", 188)
            _booktabs(page, rows, y=200, col_w=90, width=200)  # rules at y 200, 216 and 328, x 72-272
            _caption(page, "Table 2. Right column", 260, x=340)  # level with the table body, no rules under it

        (table,) = _tables(_new_pdf(tmp_path, draw), strategy="text")
        assert table["caption"] == "Table 1. Left column"
        assert len(_filled(table["rows"])) == 7

    def test_rules_drawn_in_pieces_are_joined(self, tmp_path):
        def draw(page, n):
            for start in (72, 172, 272):  # three pieces, 2 pt apart
                page.draw_line((start, 300), (start + 98, 300), width=0.8)
            page.draw_line((72, 330), (200, 330), width=0.8)  # one piece, 128 pt: wider than 15% of the page
            page.draw_line((72, 360), (100, 360), width=0.8)  # 28 pt: under 15% of the page, not a rule

        doc = pymupdf.open(_new_pdf(tmp_path, draw))
        rules = pdf_tables._page_rules(doc[0])
        doc.close()
        assert [(round(y), round(x0), round(x1)) for y, x0, x1 in rules] == [(300, 72, 370), (330, 72, 200)]

    def test_narrow_pieces_join_into_a_rule_but_a_dashed_line_is_not_a_rule(self, tmp_path):
        """Pieces of RULE_PIECE_MIN_WIDTH or more join first; the joined rule must then span 15% of the page."""
        def draw(page, n):
            for start in (72, 114, 156, 198):  # four 40 pt pieces (each under 15% of the page), 2 pt apart
                page.draw_line((start, 300), (start + 40, 300), width=0.8)
            for start in range(72, 372, 10):  # a dashed line: 8 pt dashes, 2 pt gaps
                page.draw_line((start, 330), (start + 8, 330), width=0.8)

        doc = pymupdf.open(_new_pdf(tmp_path, draw))
        rules = pdf_tables._page_rules(doc[0])
        doc.close()
        assert [(round(y), round(x0), round(x1)) for y, x0, x1 in rules] == [(300, 72, 238)]


class TestPages:
    def test_a_page_with_no_table_gives_an_empty_list(self, tmp_path):
        def draw(page, n):
            page.insert_text((72, 100), "Only prose on this page, no table at all.", fontsize=10)

        path = _new_pdf(tmp_path, draw)
        for strategy in ("lines", "text"):
            data = pdf_tables.tables_for_pdf(path, pages=[1], strategy=strategy)
            assert data == {"strategy": strategy, "pages": [{"page": 1, "tables": []}]}

    def test_every_requested_page_is_listed_in_order(self, tmp_path):
        def draw(page, n):
            if n == 2:
                _grid(page, ALLOYS)

        path = _new_pdf(tmp_path, draw, pages=3)
        data = pdf_tables.tables_for_pdf(path, pages=[3, 2, 1])
        assert [p["page"] for p in data["pages"]] == [3, 2, 1]
        assert [len(p["tables"]) for p in data["pages"]] == [0, 1, 0]
        assert data["pages"][1]["tables"][0]["id"] == "p2-t1"

    def test_pages_none_means_every_page(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: None, pages=3)
        data = pdf_tables.tables_for_pdf(path, pages=None)
        assert [p["page"] for p in data["pages"]] == [1, 2, 3]

    def test_a_page_beyond_the_document_raises(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: None, pages=2)
        with pytest.raises(ValueError, match="Page 3 out of range"):
            pdf_tables.tables_for_pdf(path, pages=[1, 3])

    def test_an_unknown_strategy_raises(self, tmp_path):
        path = _new_pdf(tmp_path, lambda page, n: None)
        with pytest.raises(ValueError, match="strategy"):
            pdf_tables.tables_for_pdf(path, pages=[1], strategy="words")


class TestStdout:
    def test_nothing_goes_to_stdout(self, tmp_path, capfd, monkeypatch):
        """PyMuPDF prints an advert on its first find_tables call; it must not reach --json output."""
        monkeypatch.setattr(pymupdf, "_recommend_layout", True, raising=False)

        def draw(page, n):
            _caption(page, "Table 1. Alloy limits", 188)
            _booktabs(page, PLAIN)
            _grid(page, ALLOYS, y=400)

        path = _new_pdf(tmp_path, draw)
        capfd.readouterr()
        for strategy in ("lines", "text"):
            monkeypatch.setattr(pymupdf, "_recommend_layout", True, raising=False)
            pdf_tables.tables_for_pdf(path, pages=[1], strategy=strategy)
        captured = capfd.readouterr()
        assert captured.out == ""

    def test_the_advert_is_real_without_the_redirect(self, tmp_path, capfd, monkeypatch):
        """Guards the test above: with the redirect removed, the advert does reach stdout."""
        monkeypatch.setattr(pymupdf, "_recommend_layout", True, raising=False)
        path = _new_pdf(tmp_path, lambda page, n: _grid(page, ALLOYS))
        capfd.readouterr()
        doc = pymupdf.open(path)
        doc[0].find_tables(strategy="lines")
        doc.close()
        out = capfd.readouterr().out
        if "pymupdf_layout" not in out:
            pytest.skip("this PyMuPDF prints no layout advert")
        assert "pymupdf_layout" in out


class TestTableDict:
    @staticmethod
    def _fake(names, rows, *, external, bbox=(72, 200, 432, 266)):
        return SimpleNamespace(
            bbox=bbox,
            header=SimpleNamespace(names=names, external=external),
            extract=lambda: rows,
        )

    def test_none_header_names_become_empty_strings_and_none_cells_stay_none(self):
        table = self._fake(["Alloy", None], [["Alloy", None], ["Inconel", None], ["Haynes", "1150"]], external=False)
        out = pdf_tables._table_dict(table, None, "p3-t2", PAGE_W, PAGE_H)
        assert out["header"] == ["Alloy", ""]
        assert out["rows"] == [["Inconel", None], ["Haynes", "1150"]]
        assert out["id"] == "p3-t2"

    def test_an_external_header_keeps_every_extracted_row(self):
        table = self._fake(["Alloy", "Temp"], [["Inconel", "650"], ["Haynes", "1150"]], external=True)
        out = pdf_tables._table_dict(table, "Table 1. X", "p1-t1", PAGE_W, PAGE_H)
        assert out["rows"] == [["Inconel", "650"], ["Haynes", "1150"]]
        assert out["caption"] == "Table 1. X"

    def test_cell_and_header_text_go_through_normalize_text(self):
        en_dash, ligature = chr(0x2013), chr(0xFB01)
        table = self._fake(
            [f"Ra{ligature}ne\nrange", "Temp"],
            [["x", "y"], [f"1300{en_dash}1600", "a  b"]],
            external=False,
        )
        out = pdf_tables._table_dict(table, None, "p1-t1", PAGE_W, PAGE_H)
        assert out["header"] == ["Rafine range", "Temp"]
        assert out["rows"] == [["1300-1600", "a b"]]

    def test_the_box_is_clamped_to_the_page(self):
        table = self._fake(["A"], [["A"]], external=False, bbox=(-5, -5, PAGE_W + 9, PAGE_H + 9))
        out = pdf_tables._table_dict(table, None, "p1-t1", PAGE_W, PAGE_H)
        assert out["bbox"] == [0.0, 0.0, 1.0, 1.0]
        assert out["rect_arg"] == "0.0000,0.0000,1.0000,1.0000"


class TestCaptionPattern:
    @pytest.mark.parametrize("line", [
        "Table 1. Alloys", "TABLE 2.-ABRADABLE", "Table 3-4 Wear", "table A2: limits",
        "Table IV. Results", "Table 5a Notes", "Table 6.1 Flow",
    ])
    def test_matches(self, line):
        assert pdf_tables._CAPTION_RE.match(line)

    @pytest.mark.parametrize("line", ["Tables 1 and 2 show", "The Table 1 above", "Table of contents", "Tabulated 3"])
    def test_rejects(self, line):
        assert not pdf_tables._CAPTION_RE.match(line)


class TestMarkdown:
    DATA = {
        "strategy": "lines",
        "pages": [
            {"page": 4, "tables": []},
            {"page": 5, "tables": [{
                "id": "p5-t1", "bbox": [0.1, 0.2, 0.5, 0.1], "rect_arg": "0.1000,0.2000,0.5000,0.1000",
                "header": ["Alloy", "Note"], "rows": [["Inconel", "a|b"], ["Haynes", None]],
                "caption": "Table 1. Alloys",
            }, {
                "id": "p5-t2", "bbox": [0.1, 0.5, 0.5, 0.1], "rect_arg": "0.1000,0.5000,0.5000,0.1000",
                "header": ["X"], "rows": [["1"]], "caption": None,
            }]},
        ],
    }

    def test_one_pipe_table_per_table_under_a_page_heading(self):
        text = pdf_tables.format_tables_markdown(self.DATA)
        assert text.startswith("## Page 4\n\n(no tables)\n")
        assert "## Page 5\n" in text
        assert "Table 1. Alloys  [p5-t1, rect 0.1000,0.2000,0.5000,0.1000]" in text
        assert "| Alloy | Note |\n| --- | --- |\n| Inconel | a\\|b |\n| Haynes |  |\n" in text
        assert "(no caption)  [p5-t2, rect 0.1000,0.5000,0.5000,0.1000]" in text
        assert text.endswith("| 1 |\n")

    def test_no_pages_gives_a_newline(self):
        assert pdf_tables.format_tables_markdown({"strategy": "lines", "pages": []}) == "\n"
