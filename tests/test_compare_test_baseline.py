"""`scripts/compare_test_baseline.py`, the CI gate against known failures.

Pytest 9 appends " - <message>" to a FAILED/ERROR summary line whenever
CI is set, which GitHub Actions always does. The baseline file holds
bare test ids from a run with CI unset. These tests pin the cut that
makes the two agree, and the pass/fail/exit-code rules around it.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "compare_test_baseline.py"
_spec = importlib.util.spec_from_file_location("compare_test_baseline", SCRIPT_PATH)
compare_test_baseline = importlib.util.module_from_spec(_spec)
sys.modules["compare_test_baseline"] = compare_test_baseline
_spec.loader.exec_module(compare_test_baseline)

normalize_line = compare_test_baseline.normalize_line
main = compare_test_baseline.main


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_normalize_line_cuts_at_the_first_dash():
    line = "FAILED tests/test_fulltext_cache.py::test_x - ModuleNotFoundError: no chromadb"
    assert normalize_line(line) == "FAILED tests/test_fulltext_cache.py::test_x"


def test_normalize_line_with_no_dash_passes_through_stripped():
    line = "  FAILED tests/test_fulltext_cache.py::test_x  "
    assert normalize_line(line) == "FAILED tests/test_fulltext_cache.py::test_x"


def test_a_known_failure_does_not_fail_the_run(tmp_path, capsys):
    baseline = _write(tmp_path / "baseline.txt",
                       "1 failed, 1 passed in 0.1s\n"
                       "FAILED tests/test_a.py::test_known\n")
    log = _write(tmp_path / "log.txt",
                 "short test summary info\n"
                 "FAILED tests/test_a.py::test_known - AssertionError: boom\n"
                 "1 failed, 1 passed in 0.1s\n")
    exit_code_file = _write(tmp_path / "exit.txt", "1\n")

    result = main(["--baseline", str(baseline), "--log", str(log),
                   "--exit-code-file", str(exit_code_file)])

    assert result == 0
    out = capsys.readouterr().out
    assert "1 known, 0 new, 0 fixed" in out


def test_a_new_failure_fails_the_run_and_is_listed(tmp_path, capsys):
    baseline = _write(tmp_path / "baseline.txt",
                       "1 failed, 1 passed in 0.1s\n"
                       "FAILED tests/test_a.py::test_known\n")
    log = _write(tmp_path / "log.txt",
                 "short test summary info\n"
                 "FAILED tests/test_a.py::test_known - AssertionError: boom\n"
                 "FAILED tests/test_b.py::test_new - AssertionError: surprise\n"
                 "2 failed, 1 passed in 0.1s\n")
    exit_code_file = _write(tmp_path / "exit.txt", "1\n")

    result = main(["--baseline", str(baseline), "--log", str(log),
                   "--exit-code-file", str(exit_code_file)])

    assert result == 1
    out = capsys.readouterr().out
    assert "1 known, 1 new, 0 fixed" in out
    assert "tests/test_b.py::test_new" in out


def test_a_fixed_failure_is_reported_but_does_not_fail_the_run(tmp_path, capsys):
    baseline = _write(tmp_path / "baseline.txt",
                       "2 failed, 1 passed in 0.1s\n"
                       "FAILED tests/test_a.py::test_known\n"
                       "FAILED tests/test_a.py::test_now_passes\n")
    log = _write(tmp_path / "log.txt",
                 "short test summary info\n"
                 "FAILED tests/test_a.py::test_known - AssertionError: boom\n"
                 "1 failed, 2 passed in 0.1s\n")
    exit_code_file = _write(tmp_path / "exit.txt", "1\n")

    result = main(["--baseline", str(baseline), "--log", str(log),
                   "--exit-code-file", str(exit_code_file)])

    assert result == 0
    out = capsys.readouterr().out
    assert "1 known, 0 new, 1 fixed" in out
    assert "tests/test_a.py::test_now_passes" in out


def test_an_unexpected_exit_code_fails_the_run_without_reading_the_baseline(tmp_path, capsys):
    baseline = _write(tmp_path / "baseline.txt", "0 passed in 0.1s\n")
    log = _write(tmp_path / "log.txt", "INTERNALERROR> something went very wrong\n")
    exit_code_file = _write(tmp_path / "exit.txt", "3\n")

    result = main(["--baseline", str(baseline), "--log", str(log),
                   "--exit-code-file", str(exit_code_file)])

    assert result == 1
    out = capsys.readouterr().out
    assert "pytest exited 3" in out
