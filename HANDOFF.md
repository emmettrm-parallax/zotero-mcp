# Handoff: Zotero access for Claude (2026-09-29)

Read this first in a new Claude Code session. Then run the three checks in section 3.

## 1. State

| Thing | Where | State |
| --- | --- | --- |
| Zotero desktop 10.0.4 | `/Applications/Zotero.app`, data in `~/Zotero` | local API on (profile pref `httpServer.localAPI.enabled`) |
| MCP server + CLI package | editable uv tool from `~/Github/zotero-mcp` (fork of 54yyyu/zotero-mcp 0.13.1) | installed, executables `zotero-mcp`, `zotero-cli` in `~/.local/bin` |
| MCP entry | `~/.claude.json`, server name `zotero`, `ZOTERO_LOCAL=true`, `ZOTERO_MCP_TOOLSETS=none` | registered, 35 tools, loads on session restart |
| Generic CLI skill | `~/.claude/skills/zotero-cli/` | installed by `zotero-mcp install-skill` |
| Meridian skill | `Meridian-Network/.claude/skills/zotero-research/SKILL.md` | written, untracked on `main`, contract test passes |
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
