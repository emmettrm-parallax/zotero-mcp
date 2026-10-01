"""`_fact_lead_line`, the one-line markdown form of a fact record in `index search` output.

D2 adds a `lead` key to a fact lead record that has no quantity and no value
(`index_grep.project_index`). This file pins the line `_fact_lead_line` prints for that case,
separately from the key-set tests in `test_index_grep.py`.
"""

from zotero_mcp.cli_standalone import _fact_lead_line


def test_a_fact_with_quantity_and_value_prints_the_usual_line():
    fact = {"page": 5, "id": "F0012", "quantity": "leakage rate", "value": "5.34", "unit": "g/s"}
    assert _fact_lead_line(fact) == "p5 F0012 leakage rate = 5.34 g/s"


def test_a_fact_with_no_quantity_and_no_value_prints_the_lead_in_quotes():
    fact = {"page": 5, "id": "F0012", "quantity": None, "value": None,
            "lead": "The seal ring loses contact under thermal growth ..."}
    assert _fact_lead_line(fact) == 'p5 F0012 "The seal ring loses contact under thermal growth ..."'


def test_a_fact_with_no_lead_key_still_prints_the_dash_form():
    fact = {"page": 5, "id": "F0012", "quantity": None, "value": None}
    assert _fact_lead_line(fact) == "p5 F0012 - = -"


def test_the_lead_form_leaves_out_unit_condition_and_ref():
    fact = {"page": 5, "id": "F0012", "quantity": None, "value": None, "lead": "A short lead.",
            "unit": "mm", "condition": "cold", "ref": "Table 2"}
    assert _fact_lead_line(fact) == 'p5 F0012 "A short lead."'


def test_the_lead_form_keeps_the_page_and_id_format():
    fact = {"page": 12, "id": "F0099", "quantity": None, "value": None, "lead": "Lead text."}
    assert _fact_lead_line(fact) == 'p12 F0099 "Lead text."'
