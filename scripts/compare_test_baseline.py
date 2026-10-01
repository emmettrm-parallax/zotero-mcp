#!/usr/bin/env python3
"""Compare a pytest run against the fork's known-failure baseline.

Pytest 9 appends " - <message>" to a FAILED or ERROR line in the short
test summary whenever the CI environment variable is set. GitHub Actions
sets CI, so a workflow run carries that suffix. The baseline in
fork_test_baseline.txt holds bare test ids, from a run with CI unset.
This script cuts the suffix off both sides before it compares, so the
two agree on the same node id.

Usage:

    python3 scripts/compare_test_baseline.py \\
        --baseline fork_test_baseline.txt \\
        --log pytest_output.txt \\
        --exit-code-file pytest_exit_code.txt

All three flags default to the paths the test workflow writes, so a
plain `python3 scripts/compare_test_baseline.py` run from the repo root
reads the files a CI run just produced.
"""

import argparse
import re
import sys

# A FAILED or ERROR line from the short test summary pytest prints at
# the end of a run. This pattern never matches the "ERROR collecting
# ..." header pytest prints earlier, in the verbose traceback section.
LINE_RE = re.compile(r"^(FAILED|ERROR)\s+\S")
SUMMARY_MARKER = "short test summary info"


def normalize_line(line):
    """Cut a FAILED/ERROR line at the first " - " and strip it.

    The kept part is the status word and the test node id. A line with
    no " - " has nothing to cut, so it passes through stripped only.
    """
    head, _sep, _tail = line.strip().partition(" - ")
    return head.strip()


def read_baseline(path):
    """Read the known FAILED/ERROR lines from a baseline file.

    Line 1 of the file is a human summary line, not a known test id, so
    it is not part of the returned set.
    """
    with open(path, encoding="utf-8") as f:
        lines = [raw_line.rstrip("\n") for raw_line in f]
    return {normalize_line(raw_line) for raw_line in lines[1:] if raw_line.strip()}


def read_observed(path):
    """Read the FAILED/ERROR lines a pytest text log reports.

    Only lines at or after the short test summary marker count. The
    same status words appear earlier, inside the verbose traceback
    section, and must not be counted twice.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    tail = text.split(SUMMARY_MARKER, 1)[1] if SUMMARY_MARKER in text else text
    return {normalize_line(raw_line) for raw_line in tail.splitlines() if LINE_RE.match(raw_line)}


def read_exit_code(path):
    with open(path, encoding="utf-8") as f:
        return int(f.read().strip())


def compare(baseline, observed):
    """Split the two normalized sets into known, new and fixed lines.

    A known line appears in both sets. A new line appears only in the
    observed set, so it is a regression the baseline does not cover. A
    fixed line appears only in the baseline, so the failure it recorded
    no longer reproduces.
    """
    known = sorted(baseline & observed)
    new = sorted(observed - baseline)
    fixed = sorted(baseline - observed)
    return known, new, fixed


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", default="fork_test_baseline.txt",
                         help="Path to the known-failure baseline file")
    parser.add_argument("--log", default="pytest_output.txt",
                         help="Path to the captured pytest text output")
    parser.add_argument("--exit-code-file", default="pytest_exit_code.txt",
                         help="Path to a file holding the pytest process exit code")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    exit_code = read_exit_code(args.exit_code_file)
    # 0 means every test passed. 1 means some tests failed or errored,
    # the case this gate exists to tolerate. Any other code means
    # pytest did not finish a normal run, so the baseline comparison
    # below cannot be trusted.
    if exit_code not in (0, 1):
        print(f"pytest exited {exit_code}, not a normal pass or fail run.")
        print("Treating this as a failure. Tail of the log:")
        with open(args.log, encoding="utf-8") as f:
            lines = f.readlines()
        print("".join(lines[-40:]))
        return 1

    baseline = read_baseline(args.baseline)
    observed = read_observed(args.log)
    known, new, fixed = compare(baseline, observed)

    print(f"{len(known)} known, {len(new)} new, {len(fixed)} fixed FAILED/ERROR line(s).")

    if fixed:
        print("Fixed lines, no longer reproduced. Consider updating the baseline:")
        for line in fixed:
            print(f"  {line}")

    if new:
        print(f"New line(s) not in {args.baseline}:")
        for line in new:
            print(f"  {line}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
