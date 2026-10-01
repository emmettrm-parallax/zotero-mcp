# zotero-cli command reference

Generated from the CLI's own argument parser by
`scripts/gen_skill_reference.py` -- do not edit by hand.

Every command also accepts `--json` (machine-readable envelope on stdout) and
`-v` (diagnostics on stderr). Both are defined on the top-level parser and on
each first-level command, so they may precede the command name or follow it,
but not follow a sub-command: `get --json metadata KEY` parses and
`get metadata --json KEY` does not. The exceptions are `index push`,
`index show`, `index search`, `index cards` and `index cards push`, which
also take `--json` after the sub-command. Run `zotero-cli --json-schema`
for the output contract.


## `config`

 - `--show-secrets` -- Show full API keys

## `search (alias: s)`

 - `<query>` -- Search query
 - `--mode` -- one of `items`, `tag`, `citekey`, `advanced`, `semantic`, `notes` -- default `items` -- Search mode (default: items)
 - `--qmode` -- one of `titleCreatorYear`, `everything` -- default `titleCreatorYear`
 - `--collection` -- Scope to a collection key
 - `--limit` -- default `10`
 - `--conditions` -- JSON conditions for advanced mode
 - `--join-mode` -- one of `all`, `any` -- default `all`
 - `--sort-by`
 - `--sort-direction` -- one of `asc`, `desc` -- default `asc`
 - `--filters` -- JSON filters for semantic mode
 - `--all-libraries` -- Search every accessible library at once instead of the active one, labelling each result with its library (items, advanced and semantic modes). Requires the SQLite backend (the default in local mode).
 - `--detail` -- one of `keys_only`, `summary`, `full` -- default `summary` -- How much of each item --json returns (no effect on markdown output)

## `get (alias: g)`

### `get metadata`

 - `<item_key>`
 - `--no-abstract`
 - `--output-format` -- one of `markdown`, `bibtex` -- default `markdown`

### `get fulltext`

 - `<item_key>`

### `get bibtex`

 - `<item_key>`

### `get collections`

 - `--limit` -- default `500`

### `get collection-items`

 - `<collection_key>`
 - `--detail` -- one of `keys_only`, `summary`, `full` -- default `summary`
 - `--limit` -- default `50`
 - `--offset` -- Index of the first item to return, for paging a collection larger than --limit

### `get children`

 - `<item_key>`
 - `--item-keys` -- Comma-separated keys for batch mode

### `get tags`

 - `--limit` -- default `500`

### `get recent`

 - `--limit` -- default `10`
 - `--collection`

### `get libraries`

### `get feeds`

### `get feed-items`

 - `<library_id>`
 - `--limit` -- default `20`

## `annotations (alias: ann)`

### `annotations list`

 - `--item-key`
 - `--pdf-extraction`
 - `--limit` -- default `100`
 - `--format` -- one of `markdown`, `json` -- default `markdown`

### `annotations update`

 - `<annotation_key>`
 - `--text`
 - `--comment`
 - `--color`
 - `--add-tags` -- Comma-separated tags to add
 - `--remove-tags` -- Comma-separated tags to remove

### `annotations delete`

 - `<annotation_key>`

### `annotations create`

 - `--attachment-key` -- **required**
 - `--page` -- **required**
 - `--text` -- Exact text to highlight
 - `--rect` -- Area box x,y,width,height, normalized 0-1; `zotero-cli layout` prints boxes for figures and tables
 - `--note` -- Sticky note centered at x,y, normalized 0-1; its text is --comment
 - `--comment`
 - `--color` -- default `#ffd400` -- Hex, or a Zotero color name: yellow, red, green, blue, purple, magenta, orange, gray
 - `--tags` -- Comma-separated tags

### `annotations batch`

 - `--attachment-key` -- **required** -- Attachment for lines that do not name their own
 - `--file` -- default `-` -- JSON Lines (or a JSON array) of {page, text|rect|note, comment, color, tags}; - reads stdin
 - `--dry-run` -- Locate every highlight and report what it would cover, without writing

## `layout`

 - `<attachment_key>`
 - `--pages` -- default `all` -- Pages to scan: all (default), 3, 3-6, or 1,4,6-9

## `grep`

 - `<key>` -- Item key or PDF attachment key
 - `<terms>` -- Terms to find. Each is counted on its own
 - `--regex` -- Treat each TERM as a regex
 - `--word` -- Match whole words only
 - `--pages` -- default `all` -- Pages to search: all (default), 3, 3-6, or 1,4,6-9
 - `--context` -- default `300` -- Characters of context on each side of a match
 - `--max-hits` -- default `200` -- Cap on snippets returned. Counts always cover every hit
 - `--order` -- one of `page`, `score` -- default `page` -- Order pages by number, or by hit density
 - `--no-cache` -- Do not use the page-text cache
 - `--jobs` -- Worker processes for text extraction
 - `--text` -- Print one 'pN: snippet' line per hit instead of markdown. Does not combine with --json

## `sections`

 - `<key>` -- Item key or PDF attachment key
 - `--pages` -- default `all` -- Pages to cover: all (default), 3, 3-6, or 1,4,6-9
 - `--max-level` -- default `2` -- Deepest outline level that starts a section
 - `--chunk-pages` -- default `8` -- Longest section in pages. Also the chunk size with no outline
 - `--inventory` -- List the tables, figures and equations in each section

## `tables`

 - `<key>` -- Item key or PDF attachment key
 - `--pages` -- **required** -- Pages to read: all, 3, 3-6, or 1,4,6-9
 - `--strategy` -- one of `lines`, `text` -- default `lines` -- lines finds ruled tables. text finds tables under a 'Table N' caption that have no ruled cells

## `index`

### `index push`

 - `<key>` -- Item key or PDF attachment key
 - `--from` -- **required** -- Index JSON file. Use - to read stdin
 - `--replace` -- Replace the item's existing index instead of failing
 - `--tags` -- Comma-separated tags to add to the parent item
 - `--dry-run` -- Report what would be written, without writing

### `index show`

 - `<key>` -- Item key or PDF attachment key
 - `--section` -- Only this section (S03) and its entries
 - `--pages` -- Only entries on these pages: 3, 3-6, or 1,4,6-9 (with --section, both apply)
 - `--grep` -- Only entries that match TERM[,TERM]. Repeat the flag to add terms. Any term matches.
 - `--regex` -- Treat each --grep value as one regex
 - `--fields` -- one of `lead`, `full` -- lead keeps a few short fields per entry, full the whole record (default full)
 - `--expand` -- Also match the variants and symbols of the vocabulary entries that match a --grep term
 - `--limit` -- Most entries of each kind to return, best matches first. Default 40 with --grep or --fields. 0 means no cap.

### `index search`

 - `<terms>` -- Terms to find. Each value splits on commas, so quote a term that has spaces.
 - `--items` -- Item or PDF attachment keys to search, instead of every item with --tag
 - `--tag` -- default `status/indexed,status/index-failed-gate` -- Search every item that has this tag, or a comma-separated list of tags (ignored with --items). An index that failed the audit gate still holds facts, so recall searches both tags by default.
 - `--fields` -- one of `lead`, `full` -- default `lead` -- lead keeps a few short fields per entry, full the whole record
 - `--expand` -- Also match the variants and symbols of the vocabulary entries that match a TERM
 - `--limit` -- default `10` -- Most entries of each kind to return for each item. 0 means no cap.
 - `--max-items` -- default `10` -- Most items with hits to return, most facts first. 0 means no cap.
 - `--regex` -- Treat each TERM as one regex

### `index cards`

 - `--grep` -- Only cards that match TERM[,TERM] in any field. Repeat the flag to add terms.
 - `--regex` -- Treat each --grep value as one regex
 - `--expand` -- Print the full card instead of the one-line compact form

#### `index cards push`

 - `<key>` -- Item key or PDF attachment key
 - `--from` -- default `-` -- Card JSON file. Use - to read stdin, the default.

## `notes (alias: n)`

### `notes list`

 - `--item-key`
 - `--limit` -- default `20`
 - `--full`
 - `--raw-html`

### `notes create`

 - `--item-key` -- **required**
 - `--title`
 - `--text` -- Note text (use - to read from stdin)
 - `--tags`

### `notes update`

 - `--item-key` -- **required**
 - `--text` -- New text (use - for stdin)

### `notes delete`

 - `--item-key` -- **required**

## `add`

### `add doi`

 - `<doi>`
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)
 - `--attach-mode` -- one of `auto`, `linked_url`, `import_file`, `none`, `required` -- default `auto`

### `add url`

 - `<url>`
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)
 - `--attach-mode` -- one of `auto`, `linked_url`, `import_file`, `none`, `required` -- default `auto`

### `add file`

 - `--filepath` -- **required**
 - `--title` -- Override title if metadata extraction misses
 - `--item-type` -- default `document` -- Zotero item type for the new item (default: document)
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)

### `add isbn`

 - `<isbn>`
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)

### `add bibtex`

 - `--bibtex` -- Inline BibTeX (use - to read from stdin)
 - `--file` -- Path to a .bib/.bibtex file
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)
 - `--attach-mode` -- one of `auto`, `linked_url`, `import_file`, `none`, `required` -- default `auto`

### `add csl-json`

 - `--json` -- Inline CSL JSON (use - to read from stdin)
 - `--file` -- Path to a .json/.csljson file
 - `--collections` -- Comma-separated collection keys, names, or paths
 - `-c, --collection` -- Collection key, name, or parent/child path (repeatable; not comma-split, so names with commas work)
 - `--tags` -- Comma-separated tags
 - `--if-exists` -- one of `file`, `skip`, `duplicate` -- default `file` -- When the item already exists: 'file' (default) reuses it and adds missing collections/tags; 'skip' leaves it untouched; 'duplicate' creates a new item anyway
 - `--create-collections` -- Create collections that don't exist yet (including parent/child paths)
 - `--attach-mode` -- one of `auto`, `linked_url`, `import_file`, `none`, `required` -- default `auto`

## `collections (alias: coll)`

### `collections create`

 - `<name>`
 - `--parent`

### `collections update`

 - `<collection_key>`
 - `--name` -- New name
 - `--parent` -- Key or name of the new parent collection
 - `--top-level` -- Move out of any parent collection

### `collections search`

 - `<query>`

### `collections manage`

 - `--item-keys` -- **required**
 - `--add-to`
 - `--remove-from`

## `tags`

 - `--query`
 - `--tag`
 - `--add`
 - `--remove`
 - `--limit` -- default `50`

## `edit`

 - `<item_key>`
 - `--title`
 - `--creators` -- JSON array of creators
 - `--date`
 - `--publication-title`
 - `--abstract`
 - `--tags` -- Replace all tags (comma-separated)
 - `--add-tags`
 - `--remove-tags`
 - `--collections` -- Add to collections (comma-separated keys)
 - `--collection-names` -- Add to collections (comma-separated names)
 - `--doi`
 - `--url`
 - `--extra`
 - `--volume`
 - `--issue`
 - `--pages`
 - `--publisher`
 - `--issn`
 - `--language`
 - `--short-title`
 - `--edition`
 - `--isbn`
 - `--book-title`

## `duplicates`

### `duplicates find`

 - `--method` -- one of `title`, `doi`, `both` -- default `both`
 - `--collection`
 - `--limit` -- default `50`

### `duplicates merge`

 - `--keeper-key` -- **required**
 - `--duplicate-keys` -- **required**
 - `--dry-run`

## `db`

### `db update`

 - `--force-rebuild`
 - `--limit`
 - `--fulltext`
 - `--allow-mass-deletion`
 - `--config-path`
 - `--db-path`
 - `--openai-batch` / `--no-openai-batch` -- mutually exclusive

### `db batch-status`

 - `--batch-id`
 - `--config-path`

### `db batch-import`

 - `--batch-id`
 - `--config-path`

### `db status`

 - `--config-path`

### `db inspect`

 - `--limit` -- default `20`
 - `--filter-text`
 - `--show-documents`
 - `--stats`
 - `--config-path`

## `library`

 - `<action>` -- one of `switch`, `list`, `reset`
 - `--library-id`
 - `--library-type` -- one of `user`, `group` -- default `group`

## `outline`

 - `<item_key>`

## `read`

 - `<item_key>`
 - `--start-page` -- **required**
 - `--end-page` -- Defaults to --start-page (a single page)
 - `--format` -- one of `text`, `image` -- default `text` -- image writes PNG page images (up to 10 pages) for math, figures and tables
 - `--rect` -- With --format image: crop the start page to x,y,width,height (normalized 0-1), e.g. from `zotero-cli layout`
 - `--out` -- With --format image: directory for the PNG files (default: a new temporary directory)
 - `--text` -- Print plain page text with '--- page N ---' separators, no title header. Needs --format text. Does not combine with --json

## `attach`

 - `<item_key>`
 - `--file` -- Path to a local file to upload
 - `--url` -- URL to attach as a link
 - `--filename` -- Override the stored filename

## `delete`

### `delete item`

 - `<item_key>`
 - `--allow-note` -- Permit deleting a note (refused otherwise, since a note is usually deleted by mistake)

### `delete collection`

 - `<collection_key>`

### `delete annotation`

 - `<annotation_key>`

## `export`

 - `--item-keys` -- Comma-separated item keys
 - `--collection` -- Export a whole collection instead
 - `--style` -- default `apa` -- CSL style (default: apa)
 - `--format` -- one of `bib`, `citation`, `bibtex` -- default `bib`

## `related`

 - `<identifier>` -- DOI, arXiv ID, or Zotero item key
 - `--direction` -- one of `references`, `citations`, `both` -- default `both`
 - `--limit` -- default `20`

## `coverage`

 - `--collection` -- Scope to one collection
 - `--limit` -- default `200`

## `synthesize`

 - `--collection`
 - `--tag` -- Comma-separated tags to scope by
 - `--limit` -- default `200`
 - `--format` -- one of `markdown`, `json` -- default `markdown`

## `path`

 - `<item_key>`

## `batch`

 - `--item-keys` -- Comma-separated item keys
 - `--query` -- Select items by search query instead
 - `--tag` -- Comma-separated tags to select by
 - `--add-tags` -- Comma-separated tags to add
 - `--remove-tags` -- Comma-separated tags to remove
 - `--set` -- JSON object of Extra keys to set
 - `--remove-keys` -- Comma-separated Extra keys to remove
 - `--limit` -- default `50`
