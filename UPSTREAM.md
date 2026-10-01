# Upstream

This fork tracks [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp).
The `origin` remote points at it. The `fork` remote points at
`emmettrm-parallax/zotero-mcp`, where this branch pushes.

## Base commit

`feat/source-index-tools` branched from upstream commit
`1ac692e61239a48d43cbf8351aab1974a7b8f698`. That commit is 21 commits
past the `v0.13.1` tag. Find it again with:

```bash
git fetch origin
git merge-base origin/main HEAD
git describe --tags 1ac692e61239a48d43cbf8351aab1974a7b8f698
```

The second command confirms the commit. The third prints
`v0.13.1-21-g1ac692e`.

## Fork-only files

These files exist only in the fork. Upstream has none of them, so a
rebase or a merge never touches them:

- `HANDOFF.md`, `UPSTREAM.md`, `fork_test_baseline.txt`
- `.github/workflows/tests.yml`
- `scripts/apply_tag_plan.py`, `scripts/meridian_tag_plan_2026-09-28.json`
- `src/zotero_mcp/index_grep.py`, `index_slice.py`, `source_index.py`,
  `text_match.py`
- `src/zotero_mcp/pdf_grep.py`, `pdf_sections.py`, `pdf_source.py`,
  `pdf_tables.py`
- `tests/fixtures/source_index_sample.json`
- `tests/test_cli_source_commands.py`, `test_index_grep.py`,
  `test_index_slice.py`, `test_pdf_grep.py`, `test_pdf_sections.py`,
  `test_pdf_tables.py`, `test_source_index.py`

The fork also edits five upstream files: `scripts/gen_skill_reference.py`,
`cli_standalone.py`, `skills/zotero-cli/SKILL.md`, `skills/zotero-cli/reference.md`,
and `tests/fixtures/README.md`. An upstream change to one of these five
is the most likely source of a merge conflict.

## Rebase routine

Run this when upstream moves and the fork needs its fixes.

1. Fetch upstream: `git fetch origin`.
2. Merge, not rebase: `git merge origin/main`. The fork carries over
   3,700 tests and months of history. A rebase would replay every fork
   commit against a moving target and rewrite commits already pushed to
   `fork`. A merge keeps the history and asks git to resolve once.
3. Run the suite: `uv sync --extra pdf && uv run --with pytest --with
   pytest-timeout --with pytest-asyncio --with pytest-httpserver python -m
   pytest tests -q -p no:cacheprovider --continue-on-collection-errors`.
4. Compare the FAILED and ERROR lines against `fork_test_baseline.txt`.
   A new one is a real regression from the merge. A line that no longer
   appears means a known failure is fixed upstream. Update the baseline.
5. Push the merge commit: `git push fork feat/source-index-tools`.
