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
- `scripts/apply_tag_plan.py`, `scripts/meridian_tag_plan_2026-09-28.json`,
  `scripts/compare_test_baseline.py`
- `src/zotero_mcp/index_grep.py`, `index_query.py`, `index_slice.py`,
  `source_card.py`, `source_index.py`, `text_match.py`
- `src/zotero_mcp/pdf_grep.py`, `pdf_sections.py`, `pdf_source.py`,
  `pdf_tables.py`
- `src/zotero_mcp/tools/index_tools.py`
- `tests/fixtures/source_index_sample.json`
- `tests/live/test_source_card_live.py`
- `tests/test_cli_source_commands.py`, `test_cli_text_output.py`,
  `test_compare_test_baseline.py`, `test_index_grep.py`,
  `test_index_lead_line.py`, `test_index_slice.py`,
  `test_mcp_index_tools.py`, `test_pdf_grep.py`, `test_pdf_sections.py`,
  `test_pdf_tables.py`, `test_source_card.py`, `test_source_index.py`

The fork also edits eleven upstream files: `README.md`, `docs/tools.md`,
`scripts/gen_skill_reference.py`, `src/zotero_mcp/cli_standalone.py`,
`src/zotero_mcp/skills/zotero-cli/SKILL.md`,
`src/zotero_mcp/skills/zotero-cli/reference.md`,
`src/zotero_mcp/tools/__init__.py`, `src/zotero_mcp/tools/read_pdf.py`,
`src/zotero_mcp/toolsets.py`, `tests/fixtures/README.md`, and
`tests/test_description_tokens.py`. An upstream change to one of these
eleven is the most likely source of a merge conflict.

## Rebase routine

Run this when upstream moves and the fork needs its fixes.

1. Fetch upstream: `git fetch origin`.
2. Merge, not rebase: `git merge origin/main`. The fork carries over
   3,700 tests and months of history. A rebase would replay every fork
   commit against a moving target and rewrite commits already pushed to
   `fork`. A merge keeps the history and asks git to resolve once.
3. Run the suite the same way the workflow does, so the log and exit
   code land where the compare script reads them:
   ```bash
   uv sync --extra pdf
   uv run --with pytest --with pytest-timeout --with pytest-asyncio \
     --with pytest-httpserver python -m pytest tests -q \
     -p no:cacheprovider --continue-on-collection-errors \
     > pytest_output.txt 2>&1
   echo "$?" > pytest_exit_code.txt
   ```
4. Compare against the baseline: `python3 scripts/compare_test_baseline.py`.
   A new line is a real regression from the merge. A fixed line means a
   known failure is fixed upstream. Update `fork_test_baseline.txt` either
   way.
5. Push the merge commit: `git push fork feat/source-index-tools`.
