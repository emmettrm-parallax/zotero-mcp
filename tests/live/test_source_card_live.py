"""Live test for `source_card.push_card` and `list_cards` against the real library.

Gated by ZOTERO_MCP_LIVE_TESTS=1 (see conftest.py), which also means zero network calls
happen at collection time. The item is K2WYW42W, one of the two library items with no
PDF, so this never collides with a source-index build. The test skips rather than
overwrites a real card when that item already carries one: proving the write path is this
test's job, not touching live data that a person is relying on. The scratch note is
trashed in a `finally`, so a failed assertion still leaves the item as it found it.

Run once with:

    ZOTERO_MCP_LIVE_TESTS=1 uv run --no-project \\
        --python ~/.local/share/uv/tools/zotero-mcp-server/bin/python \\
        --with pytest --with pytest-timeout --with pytest-asyncio --with pytest-httpserver \\
        python -m pytest tests/live/test_source_card_live.py -v
"""

from pathlib import Path

import pytest

from zotero_mcp import client as _client
from zotero_mcp.cli_standalone import CLIContext, setup_zotero_environment

ITEM_KEY = "K2WYW42W"

SCRATCH_CARD = {
    "schema": "source-card/v1",
    "item_key": ITEM_KEY,
    "title_short": "Scratch card from test_source_card_live",
    "kind": "report",
    "topics": ["scratch topic one", "scratch topic two", "scratch topic three",
               "scratch topic four", "scratch topic five"],
    "synonyms": [f"scratch synonym {i}" for i in range(1, 11)],
    "quantities": [],
    "scope": "A scratch card the live test writes and then trashes.",
    "not_about": "Anything a person relies on. This card never outlives the test run.",
    "pages": None,
    "tags": [],
}


@pytest.fixture
def live_ctx(monkeypatch):
    """A real write-capable context.

    ``tests/conftest.py`` carries an autouse fixture, ``isolate_local_write_state``,
    that points every test at an empty config path so the unit suite never reads a
    developer's real local write key. A live write test needs the opposite: this
    fixture restores the real config path, which is where the loop of this item's
    push, list and trash runs.
    """
    monkeypatch.setattr(_client, "ZOTERO_MCP_CONFIG_PATH", Path.home() / ".config" / "zotero-mcp" / "config.json")
    setup_zotero_environment()
    return CLIContext(verbose=False)


def test_push_list_and_clean_up_a_scratch_card(live_ctx):
    from zotero_mcp import source_card

    existing = source_card.list_cards(terms=None, regex=False, ctx=live_ctx)
    if any(card.get("item_key") == ITEM_KEY for card in existing["cards"]):
        pytest.skip(f"{ITEM_KEY} already carries a card: this test never overwrites live data")

    note_key = None
    try:
        result = source_card.push_card(ITEM_KEY, SCRATCH_CARD, ctx=live_ctx)
        note_key = result["note_key"]
        assert result["item_key"] == ITEM_KEY
        assert result["created"] == 1

        listed = source_card.list_cards(terms=["scratch topic"], regex=False, ctx=live_ctx)
        keys = [card.get("item_key") for card in listed["cards"]]
        assert ITEM_KEY in keys
    finally:
        if note_key:
            from zotero_mcp.tools import annotations

            outcome = annotations.delete_note(item_key=note_key, ctx=live_ctx)
            assert "Successfully trashed" in outcome, (
                f"could not trash the scratch card note {note_key}: {outcome}"
            )
