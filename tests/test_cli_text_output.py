"""Plain-text output for `zotero-cli read --text` and `grep --text`.

The default output of both commands -- markdown, or the `--json` envelope --
must not change: these tests pin the existing shape first, then the new
plain-text shapes and their refusal to combine with `--json` (and, for
`read`, with `--format image`).
"""

import argparse
import json
import re
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pymupdf
import pytest
from conftest import DummyContext

from zotero_mcp import cli_standalone, pdf_source
from zotero_mcp.cli_json import CliError
from zotero_mcp.extract import PAGE_SEPARATOR, ExtractedDoc
from zotero_mcp.pdf_grep import format_grep_text


def _args(**kwargs):
    """Namespace with real values -- a MagicMock would make every getattr
    truthy and silently enable flags the test never set."""
    defaults = dict(verbose=False, json_out=False, text=False, rect=None, out=None, format="text")
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _run_main(monkeypatch, capsys, *argv):
    """Run `zotero-cli *argv` in process. Return (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["zotero-cli", *argv])
    code = 0
    try:
        cli_standalone.main()
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# read --text
# ---------------------------------------------------------------------------

def _patch_extract(monkeypatch, page_texts, total=None, needs_ocr=()):
    """Stand in for the extraction seam `read_pdf_page_texts` shares with
    `read_pdf_text`: known page text, no real PDF or Zotero item needed."""
    from zotero_mcp.tools import read_pdf as read_pdf_mod

    total_pages = total if total is not None else len(page_texts)

    def _fake_page_count(_path):
        return total_pages

    def _fake_extract_pdf(_path, *, pages=None, max_pages=None):
        wanted = [
            p for p in (range(total_pages) if pages is None else pages)
            if 0 <= p < total_pages
        ]
        texts = [page_texts[p % len(page_texts)] for p in wanted]
        return ExtractedDoc(
            text=PAGE_SEPARATOR.join(texts), pages=tuple(texts), page_numbers=tuple(wanted),
            page_count=total_pages, source="pdf", needs_ocr=tuple(needs_ocr),
        )

    monkeypatch.setattr(read_pdf_mod, "pdf_page_count", _fake_page_count)
    monkeypatch.setattr(read_pdf_mod, "extract_pdf", _fake_extract_pdf)
    monkeypatch.setattr(read_pdf_mod, "_get_pdf_path", lambda _k, _c: ("/tmp/test.pdf", "Test Paper", False))
    monkeypatch.setattr(read_pdf_mod, "_garbled_content_flags", lambda _path, _doc: {})


class TestReadPdfPageTexts:
    """The tool function `read_pdf_page_texts` directly, below the CLI."""

    def test_page_separators_in_order_no_header(self, monkeypatch):
        from zotero_mcp.tools.read_pdf import read_pdf_page_texts

        _patch_extract(monkeypatch, ["Page one body.", "Page two body.", "Page three body."], total=3)
        text = read_pdf_page_texts("KEY00001", 1, 3, ctx=DummyContext())
        assert re.findall(r"--- page \d+ ---", text) == [
            "--- page 1 ---", "--- page 2 ---", "--- page 3 ---",
        ]
        assert "Page one body." in text
        assert "Page two body." in text
        assert "Page three body." in text
        assert not re.search(r"(?m)^#", text), "no markdown header line"
        assert "Item Key" not in text
        assert "PDF Pages" not in text

    def test_garble_note_is_kept(self, monkeypatch):
        from zotero_mcp.tools import read_pdf as read_pdf_mod
        from zotero_mcp.tools.read_pdf import read_pdf_page_texts

        _patch_extract(monkeypatch, ["Equation body."], total=1)
        monkeypatch.setattr(read_pdf_mod, "_garbled_content_flags",
                            lambda _path, _doc: {0: "> **Garbled in this text:** Equation 3"})
        text = read_pdf_page_texts("KEY00001", 1, 1, ctx=DummyContext())
        assert "Garbled in this text" in text

    def test_no_size_warning(self, monkeypatch):
        """`read_pdf_text` prepends a size warning past ~5K tokens. The plain-text
        function carries no such header for a caller piping it elsewhere."""
        from zotero_mcp.tools.read_pdf import read_pdf_page_texts

        _patch_extract(monkeypatch, ["word " * 10000], total=1)
        text = read_pdf_page_texts("KEY00001", 1, 1, ctx=DummyContext())
        assert "Response size" not in text


class TestCmdReadText:
    """`zotero-cli read --text`, dispatched through `cmd_read`."""

    def test_default_markdown_output_is_unchanged(self, monkeypatch, capsys):
        """`--text` is additive: the pre-existing markdown shape still holds
        with the flag absent, for the same fake inputs."""
        _patch_extract(monkeypatch, ["Page one body."], total=1)
        args = _args(item_key="KEY00001", start_page=1, end_page=None, text=False)
        with patch("zotero_mcp.cli_standalone.setup_zotero_environment"):
            cli_standalone.cmd_read(args)
        out = capsys.readouterr().out
        assert out.startswith("# PDF Pages 1-1 of Test Paper\n")
        assert "## Page 1" in out
        assert "Page one body." in out

    def test_text_flag_prints_plain_pages(self, monkeypatch, capsys):
        _patch_extract(monkeypatch, ["Page one.", "Page two."], total=2)
        args = _args(item_key="KEY00001", start_page=1, end_page=2, text=True)
        with patch("zotero_mcp.cli_standalone.setup_zotero_environment"):
            cli_standalone.cmd_read(args)
        out = capsys.readouterr().out
        assert "--- page 1 ---" in out
        assert "--- page 2 ---" in out
        assert not out.startswith("#")

    def test_text_and_json_is_bad_flags(self, monkeypatch):
        _patch_extract(monkeypatch, ["x"], total=1)
        with pytest.raises(CliError) as exc:
            cli_standalone.cmd_read(_args(item_key="K", start_page=1, end_page=None,
                                          text=True, json_out=True))
        assert exc.value.code == "bad_flags"

    def test_text_and_image_format_is_bad_flags(self, monkeypatch):
        _patch_extract(monkeypatch, ["x"], total=1)
        with pytest.raises(CliError) as exc:
            cli_standalone.cmd_read(_args(item_key="K", start_page=1, end_page=None,
                                          text=True, format="image"))
        assert exc.value.code == "bad_flags"


class TestCmdReadTextThroughMain:
    """End to end through `main()`, so the parser and the exit code are real."""

    def test_text_and_json_is_bad_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        code, out, _err = _run_main(
            monkeypatch, capsys, "--json", "read", "K1", "--start-page", "1", "--text",
        )
        assert code == 1
        envelope = json.loads(out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "bad_flags"

    def test_text_and_image_format_is_bad_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        code, _out, err = _run_main(
            monkeypatch, capsys, "read", "K1", "--start-page", "1", "--text", "--format", "image",
        )
        assert code == 1
        assert "Error" in err


# ---------------------------------------------------------------------------
# grep --text
# ---------------------------------------------------------------------------

WIDE = 3000  # points: a long line fits without clipping


def _make_pdf(path, pages):
    """Write a PDF. ``pages`` is a list of pages, each a list of lines."""
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page(width=WIDE, height=792)
        for number, line in enumerate(lines):
            page.insert_text((10, 60 + 14 * number), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return str(path)


def _resolved_pdf_for(path, title="Test Paper"):
    @contextmanager
    def resolved_pdf(_key, _ctx):
        yield pdf_source.ResolvedPdf("PARENT01", "ATTACH01", path, title, False)
    return resolved_pdf


class TestFormatGrepText:
    """The formatter directly, on constructed data (mirrors `TestMarkdown`)."""

    def test_one_line_per_snippet_whitespace_collapsed_brackets_kept(self):
        data = {"pages": [
            {"page": 4, "snippets": [{"text": "the [[pressure  ratio]]\nwas   high",
                                       "terms": ["pressure ratio"]}]},
            {"page": 9, "snippets": [{"text": "a [[pressure ratio]] of", "terms": ["pressure ratio"]}]},
        ]}
        text = format_grep_text(data)
        assert text.splitlines() == [
            "p4: the [[pressure ratio]] was high",
            "p9: a [[pressure ratio]] of",
        ]

    def test_empty_with_no_hits(self):
        assert format_grep_text({"pages": []}) == ""
        assert format_grep_text({}) == ""


class TestCmdGrepText:
    """`zotero-cli grep --text`, dispatched through `cmd_grep` on a real PDF."""

    def test_default_markdown_output_is_unchanged(self, tmp_path, monkeypatch, capsys):
        path = _make_pdf(tmp_path / "doc.pdf", [["a windback seal"]])
        monkeypatch.setattr(pdf_source, "resolved_pdf", _resolved_pdf_for(path))
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        args = _args(key="KEY00001", terms=["windback"], regex=False, word=False, pages="all",
                     context=60, max_hits=200, order="page", no_cache=True, jobs=None, text=False)
        cli_standalone.cmd_grep(args)
        out = capsys.readouterr().out
        assert out.startswith("# grep: Test Paper\n")
        assert "[[windback]]" in out

    def test_lines_match_pn_shape_and_hold_no_newline(self, tmp_path, monkeypatch, capsys):
        path = _make_pdf(tmp_path / "doc.pdf", [
            ["no match on this page"],
            ["a windback seal", "another windback mention"],
        ])
        monkeypatch.setattr(pdf_source, "resolved_pdf", _resolved_pdf_for(path))
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        args = _args(key="KEY00001", terms=["windback"], regex=False, word=False, pages="all",
                     context=60, max_hits=200, order="page", no_cache=True, jobs=None, text=True)
        cli_standalone.cmd_grep(args)
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines, "the fixture must produce at least one hit"
        for line in lines:
            assert re.match(r"^p\d+: \S", line)
            assert "\n" not in line

    def test_text_and_json_is_bad_flags(self, monkeypatch):
        with pytest.raises(CliError) as exc:
            cli_standalone.cmd_grep(_args(key="K", terms=["x"], regex=False, word=False, pages="all",
                                          context=60, max_hits=200, order="page", no_cache=True,
                                          jobs=None, text=True, json_out=True))
        assert exc.value.code == "bad_flags"


class TestCmdGrepTextThroughMain:
    """End to end through `main()`, so the exit code and the two streams are real."""

    def test_zero_hits_prints_nothing_and_exits_0(self, tmp_path, monkeypatch, capsys):
        path = _make_pdf(tmp_path / "doc.pdf", [["nothing related on this page"]])
        monkeypatch.setattr(pdf_source, "resolved_pdf", _resolved_pdf_for(path))
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        code, out, err = _run_main(
            monkeypatch, capsys, "grep", "KEY00001", "windback", "--text", "--no-cache",
        )
        assert code == 0
        assert out == ""
        assert err.strip() == "no hits"

    def test_max_hits_cut_goes_to_stderr(self, tmp_path, monkeypatch, capsys):
        path = _make_pdf(tmp_path / "doc.pdf", [[f"windback mention {n}" for n in range(5)]])
        monkeypatch.setattr(pdf_source, "resolved_pdf", _resolved_pdf_for(path))
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        # A small --context keeps the five hits from merging into one snippet,
        # so --max-hits 1 actually has something to cut.
        code, out, err = _run_main(
            monkeypatch, capsys, "grep", "KEY00001", "windback", "--text", "--no-cache",
            "--context", "5", "--max-hits", "1",
        )
        assert code == 0
        assert out.count("\n") == 1
        assert "cut" in err

    def test_text_and_json_is_bad_flags(self, monkeypatch, capsys):
        monkeypatch.setattr(cli_standalone, "setup_zotero_environment", lambda: None)
        code, out, _err = _run_main(
            monkeypatch, capsys, "--json", "grep", "K1", "x", "--text",
        )
        assert code == 1
        envelope = json.loads(out)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "bad_flags"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_grep_text_defaults_false(self):
        args = cli_standalone.build_parser().parse_args(["grep", "K1", "term"])
        assert args.text is False

    def test_grep_text_parses(self):
        args = cli_standalone.build_parser().parse_args(["grep", "K1", "term", "--text"])
        assert args.text is True

    def test_read_text_defaults_false(self):
        args = cli_standalone.build_parser().parse_args(["read", "K1", "--start-page", "1"])
        assert args.text is False

    def test_read_text_parses(self):
        args = cli_standalone.build_parser().parse_args(
            ["read", "K1", "--start-page", "1", "--text"],
        )
        assert args.text is True
