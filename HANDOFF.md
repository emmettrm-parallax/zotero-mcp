# Handoff: Zotero access for Claude (2026-09-29)

Read this first in a new Claude Code session. Then run the three checks in section 3.

## 1. State

| Thing | Where | State |
| --- | --- | --- |
| Zotero desktop 10.0.4 | `/Applications/Zotero.app`, data in `~/Zotero` | local API on (profile pref `httpServer.localAPI.enabled`) |
| MCP server + CLI package | editable uv tool from `~/Github/zotero-mcp` (fork of 54yyyu/zotero-mcp 0.13.1) | installed, executables `zotero-mcp`, `zotero-cli` in `~/.local/bin` |
| MCP entry | `~/.claude.json`, server name `zotero`, `ZOTERO_LOCAL=true`, `ZOTERO_MCP_TOOLSETS=none` | registered, 35 tools, loads on session restart |
| Generic CLI skill | `~/.claude/skills/zotero-cli/` | installed by `zotero-mcp install-skill` |
| Zotero skills | `~/.claude/skills/source-index`, `~/.claude/skills/zotero-research` | moved to github.com/emmettrm-parallax/zotero-skills on 2026-10-01, installed by symlink |
| Write key | Zotero profile `localAPIKeys.json` and `~/.config/zotero-mcp/config.json` | saved, reusable, no dialog |
| Library tags | 22 items, 109 tags, 74 in the controlled vocabulary | applied and verified |
| Tag plan and apply script | `~/Github/zotero-mcp/scripts/meridian_tag_plan_2026-09-28.json`, `scripts/apply_tag_plan.py` | done, kept for reuse |
| Memory note | `~/.claude/projects/-Users-emmettmoore-Github-Meridian-Network/memory/zotero-mcp-setup.md` | current |

## 2. Open scope

1. Commit the skill on a branch and open a PR. The file is new and untracked. No allowlist edit is needed.
2. Decide on the three `status/` flags: two Boyce duplicates, three misfiled items under `compressors`, one web page copy with no PDF. An agent must not merge or delete. The user decides.
3. Build the semantic index if search by meaning is wanted: `zotero-mcp update-db`. Needs the `[semantic]` extra. Without it, semantic mode returns nothing.
4. File the sourcebook artifacts into Zotero by DOI with the controlled tags. The shaft-seal, bleed, and diffuser sourcebooks hold about 400 sources. The user has not said yes yet.
5. Move to a company group library later. Each engineer then needs a personal read-only key, and the MCP entry needs `ZOTERO_LIBRARY_TYPE=group`.

## 3. Checks for a new session

```bash
zotero-cli --json config
```

```bash
zotero-cli --json search "component/seal" --mode tag --limit 5 --detail keys_only
```

```bash
zotero-mcp authorize-local --status
```

Expected: `ok: true`, three seal items, and `Write mode: local`.

## 4. Watch-outs

- The write key lives in two files that must match. Edit the Zotero copy only while Zotero is closed. Zotero reads it at start only.
- A 401 on a write means the copies drifted. Run `zotero-mcp authorize-local` and click Always Allow. That rebuilds both.
- The MCP tool schemas cost about 12k tokens on every request. Use `zotero-cli` from a shell when possible.
- One local write carries at most 50 items. Split a bigger plan.
- The tag vocabulary is in the Meridian skill. A new tag needs a table line there first.

## 5. Added 2026-09-29: source-index tools

Branch `feat/source-index-tools` is checked out here (local, not pushed). It adds `zotero-cli grep`, `sections`, `index push` and `index show`. The Meridian skills `source-index` and `zotero-research` use them. Three items carry index notes and the tag `status/indexed`: 3CKPN9EK, QSEELLWH, EBC5ZSRK. Worktrees `a0` to `a3` under `~/Github/zotero-mcp-wt/` hold the merged task branches. Open scope and audit numbers: Meridian memory note `source-index-pipeline-built`.

## 6. Added 2026-09-30: round 2

Branch `feat/source-index-tools` is pushed to `fork` at ed70464. New commands: `zotero-cli tables KEY --pages RANGE --strategy lines|text` and `index show KEY --pages RANGE`. New error code: `bad_pages`. The three items carry the b2 indexes with 7, 6 and 13 note parts. Worktrees `f1` to `f3` under `~/Github/zotero-mcp-wt/` hold the merged round-2 branches. Remove `a0` to `a3` and `f1` to `f3` after the branch merges. Audit numbers and open items: Meridian memory note `source-index-pipeline-built`.

## 7. Added 2026-09-30: round 3

Branch `feat/source-index-tools` holds the round-3 merges. It is not pushed.
New: `index show` filters an index. Example: `zotero-cli --json index show 3CKPN9EK --grep leakage --expand --fields lead`.
New: `index search` reads every indexed item. Example: `zotero-cli --json index search "windback,leakage" --expand`.
New error code: `bad_grep`. It means an empty term list, or `--expand` or `--regex` without `--grep`.
Remove the worktrees `r3f1` to `r3f3` under `~/Github/zotero-mcp-wt/` after the branch merges.
Recall study of 2026-09-30, 12 questions, one Sonnet run per cell: the old skill, the filtered calls and the new skill each answered 12 of 12. In the old condition the agents wrote the full index to a file and filtered it. It never reached the context. The filters protect a client with no shell. Next: give the MCP index tool a lead-only default, and give `read` and `grep` a plain-text output.

## 8. Added 2026-10-01: round 4

Branch `feat/source-index-tools` holds the round-4 merges at 39f12d5. Suite: 4432 passed, 0 failed.
New MCP tools in the toolset `source-index`: `zotero_index_show` and `zotero_index_search`, lead-only by default, cap 80. A bare show returns `bad_grep`.
New flags: `read --text` and `grep --text` print plain text. `index search --tag` takes a comma list. Its default is `status/indexed,status/index-failed-gate`, because an index that failed the audit gate still holds facts.
New commands: `index cards [--grep T,T] [--expand]` lists the source cards, `index cards push KEY --from FILE` writes one. Schema `source-card/v1`, error `invalid_card`. 21 items carry a card.
Fork upkeep: `.github/workflows/tests.yml` runs the suite on Python 3.11 with the `pdf` extra. `scripts/compare_test_baseline.py` compares the failed test ids with `fork_test_baseline.txt` and fails the job only on a new id. `UPSTREAM.md` pins the upstream base and gives the rebase routine. The README has a teammate install section. The first GitHub run on 2026-10-01 passed: 3821 passed on Linux, 35 known failed or error ids, 0 new.
Semantic store: the tool venv holds chromadb, sentence-transformers and torch. The config uses the `qwen` model with passage chunks. The store at `~/.config/zotero-mcp/chroma_db` holds 2128 passages. The finder trial found no accuracy gain over tags or cards on a 24-item library, so the research skill does not use it.
Library: 15 items carry an index. The 12 built this round carry `status/index-failed-gate`. Audit numbers, cost and open items: Meridian memory note `source-index-pipeline-built`.
The worktrees `a0` to `a3`, `f1` to `f3`, `r3f1` to `r3f3` and the `si4-*` set are removed. Only the main checkout remains.

## 9. Added 2026-10-01: round 5

Branch `feat/source-index-tools` holds the round-5 merges at 3e9fd96. Suite: 4477 passed, 0 new failures, CI green.
New: `split_panels(page, bbox, mask_text=False, scale=1.5)` in `pdf_layout.py` finds plot panels inside a box by whitespace cuts. Pure Python plus pymupdf. Calibrated on three library PDFs, see `tests/test_panel_split.py`.
New: `sections --inventory` lists `plots_on_page` per row: `{"page", "plots", "source", "scanned", "panel_rects"}`. The count comes from drawing boxes, from table boxes without a table label, and from grid cells in image boxes. On a scan the split runs on the whole page. The Meridian `split_units.py` reads it.
Known gap: a page with `/Rotate` 90 or 270 and a full-page image does not get the `scanned` flag. The layout frame is unrotated. 301 of 12,123 library pages.
Re-test: 8T22IS9X and CWPBGQLT were built again with the new reader prompts and the new split. Both pass the audit gate at 0.1 percent misses. Audit numbers and open items: Meridian memory note `source-index-pipeline-built`.
