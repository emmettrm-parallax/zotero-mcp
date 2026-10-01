"""Tests for split_panels: plot panels found by recursive whitespace cuts.

Every page here is a synthetic one-page PDF built with real PyMuPDF. The
raster pages hold framed panels drawn pixel by pixel, so the true place of
each panel is known to within one pixel.
"""

import random
import time

import pytest

from zotero_mcp.pdf_layout import PANEL_MASK_WORDS, _reading_order, split_panels

pymupdf = pytest.importorskip("pymupdf")

PAGE_W, PAGE_H = 612, 792
FRAME_PX = 3
TOLERANCE = 0.02
LONG_TEXT = (
    "The rotor was run at design speed with the casing treatment removed and "
    "the mass flow was throttled in small steps until the stall point was "
    "reached. Each run took the pressure ratio and the efficiency from the "
    "same four rakes and the readings were averaged over the last ten "
    "seconds of every step. "
)


def _raster_page(frames, *, background=255, jitter=0, speckle=0.0, seed=7):
    """Open a one-page PDF whose page is a raster of framed panels.

    ``frames`` are [x, y, w, h] page fractions. ``jitter`` adds uniform gray
    noise of that amplitude. ``speckle`` is the share of pixels set to a dark
    spot, as on a scan.
    """
    rng = random.Random(seed)
    count = PAGE_W * PAGE_H
    if jitter:
        table = bytes(
            min(max(background + (b % (2 * jitter + 1)) - jitter, 0), 255)
            for b in range(256)
        )
        pixels = bytearray(rng.randbytes(count).translate(table))
    else:
        pixels = bytearray([background]) * count
    for _ in range(int(count * speckle)):
        pixels[rng.randrange(count)] = 90
    for fx, fy, fw, fh in frames:
        x0, y0 = round(fx * PAGE_W), round(fy * PAGE_H)
        x1, y1 = round((fx + fw) * PAGE_W), round((fy + fh) * PAGE_H)
        for row in range(y0, y1):
            base = row * PAGE_W
            if row < y0 + FRAME_PX or row >= y1 - FRAME_PX:
                pixels[base + x0:base + x1] = bytes(x1 - x0)
            else:
                pixels[base + x0:base + x0 + FRAME_PX] = bytes(FRAME_PX)
                pixels[base + x1 - FRAME_PX:base + x1] = bytes(FRAME_PX)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, PAGE_W, PAGE_H, bytes(pixels), False)
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(page.rect, pixmap=pix)
    return doc, page


def _assert_boxes_match(found, truth):
    assert len(found) == len(truth), found
    for box, want in zip(found, truth):
        assert box == pytest.approx(want, abs=TOLERANCE), (box, want)


class TestSyntheticPanels:
    def test_two_by_two_in_reading_order(self):
        truth = [
            [0.12, 0.10, 0.34, 0.20],
            [0.54, 0.10, 0.34, 0.20],
            [0.12, 0.38, 0.34, 0.20],
            [0.54, 0.38, 0.34, 0.20],
        ]
        _doc, page = _raster_page(truth)
        found = split_panels(page, [0.05, 0.05, 0.9, 0.58])
        _assert_boxes_match(found, truth)
        for x, y, w, h in found:
            assert 0.05 <= x and x + w <= 0.95 and 0.05 <= y and y + h <= 0.63

    def test_three_by_three(self):
        truth = [
            [x, y, 0.25, 0.20]
            for y in (0.08, 0.33, 0.58)
            for x in (0.10, 0.375, 0.65)
        ]
        _doc, page = _raster_page(truth)
        found = split_panels(page, [0.0, 0.0, 1.0, 0.9])
        _assert_boxes_match(found, truth)

    def test_uniform_image_gives_no_panel(self):
        _doc, page = _raster_page([], background=200)
        assert split_panels(page, [0.0, 0.0, 1.0, 1.0]) == []

    def test_blank_page_gives_no_panel(self):
        doc = pymupdf.open()
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        assert split_panels(page, [0.0, 0.0, 1.0, 1.0], mask_text=True) == []

    def test_short_sub_caption_strip_is_not_a_panel(self):
        truth = [[0.20, 0.10, 0.60, 0.30]]
        _doc, page = _raster_page(truth)
        page.insert_text((200, 0.4 * PAGE_H + 25), "(a) Case 1 baseline", fontsize=9)
        found = split_panels(page, [0.1, 0.05, 0.8, 0.45])
        _assert_boxes_match(found, truth)

    def test_paragraph_is_a_cell_unless_masked(self):
        truth = [[0.15, 0.40, 0.70, 0.30]]
        _doc, page = _raster_page(truth)
        assert len(LONG_TEXT.split()) >= PANEL_MASK_WORDS
        assert page.insert_textbox(pymupdf.Rect(60, 60, 552, 200), LONG_TEXT * 2, fontsize=10) > 0
        _assert_boxes_match(split_panels(page, [0.0, 0.0, 1.0, 1.0], mask_text=True), truth)
        unmasked = split_panels(page, [0.0, 0.0, 1.0, 1.0])
        assert len(unmasked) >= 2
        assert unmasked[0][1] < truth[0][1]  # the paragraph sits above the panel

    def test_caption_block_is_masked_even_when_short(self):
        truth = [[0.15, 0.08, 0.70, 0.30]]
        _doc, page = _raster_page(truth)
        caption = "Figure 4: Rotor mass flow versus pressure ratio at design speed"
        assert len(caption.split()) < PANEL_MASK_WORDS
        assert page.insert_textbox(pymupdf.Rect(60, 330, 150, 460), caption, fontsize=10) > 0
        assert len(split_panels(page, [0.0, 0.0, 1.0, 1.0])) == 2
        _assert_boxes_match(split_panels(page, [0.0, 0.0, 1.0, 1.0], mask_text=True), truth)

    def test_speckled_scan_with_two_frames(self):
        truth = [[0.10, 0.15, 0.36, 0.30], [0.54, 0.15, 0.36, 0.30]]
        _doc, page = _raster_page(truth, background=200, jitter=15, speckle=0.003)
        found = split_panels(page, [0.0, 0.0, 1.0, 1.0])
        assert len(found) == 2, found
        # Specks inside a gutter's reach can widen a box a little. It must still hold its frame.
        for (x, y, w, h), (fx, fy, fw, fh) in zip(found, truth):
            assert x <= fx + 0.005 and y <= fy + 0.005
            assert x + w >= fx + fw - 0.005 and y + h >= fy + fh - 0.005
            assert w <= fw + 0.04 and h <= fh + 0.04

    def test_sub_box_result_is_in_page_fractions(self):
        frames = [
            [0.12, 0.10, 0.34, 0.20],
            [0.54, 0.10, 0.34, 0.20],
            [0.12, 0.38, 0.34, 0.20],
            [0.54, 0.38, 0.34, 0.20],
        ]
        _doc, page = _raster_page(frames)
        found = split_panels(page, [0.5, 0.07, 0.45, 0.55])
        _assert_boxes_match(found, [frames[1], frames[3]])

    @pytest.mark.parametrize(
        "bbox",
        [
            [0.2, 0.2, 0.0, 0.5],
            [0.2, 0.2, 0.5, -0.1],
            [1.2, 0.2, 0.3, 0.3],
            [0.2, 0.2, float("nan"), 0.3],
            [0.2, 0.2, 0.3],
            "not a box",
            None,
        ],
    )
    def test_degenerate_bbox_gives_no_panel(self, bbox):
        _doc, page = _raster_page([[0.1, 0.1, 0.8, 0.5]])
        assert split_panels(page, bbox) == []

    def test_bad_scale_gives_no_panel(self):
        _doc, page = _raster_page([[0.1, 0.1, 0.8, 0.5]])
        assert split_panels(page, [0.0, 0.0, 1.0, 1.0], scale=0) == []

    def test_whole_page_call_is_fast(self):
        frames = [[0.10, 0.15, 0.36, 0.30], [0.54, 0.15, 0.36, 0.30]]
        _doc, page = _raster_page(frames, background=200, jitter=15, speckle=0.003)
        page.insert_textbox(pymupdf.Rect(60, 560, 552, 700), LONG_TEXT * 2, fontsize=10)
        started = time.perf_counter()
        found = split_panels(page, [0.0, 0.0, 1.0, 1.0], mask_text=True, scale=1.5)
        assert time.perf_counter() - started < 3.0
        assert len(found) == 2


class TestReadingOrder:
    def test_ragged_tops_stay_in_one_row(self):
        left = [0.10, 0.104, 0.30, 0.20]
        right = [0.55, 0.100, 0.30, 0.20]
        below = [0.10, 0.40, 0.30, 0.20]
        assert _reading_order([below, right, left]) == [left, right, below]

    def test_tall_cell_beside_two_stacked_cells(self):
        tall = [0.10, 0.10, 0.30, 0.50]
        top = [0.55, 0.10, 0.30, 0.20]
        bottom = [0.55, 0.40, 0.30, 0.20]
        assert _reading_order([bottom, top, tall]) == [tall, top, bottom]

    def test_empty(self):
        assert _reading_order([]) == []
