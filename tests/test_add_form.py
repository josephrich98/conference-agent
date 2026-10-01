"""The website's "Add a conference" form stays in step with `conference-agent add`."""

import re
from pathlib import Path

from conference_agent.cli import add_field_schema

_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


def _quoted_names(block: str) -> set[str]:
    return set(re.findall(r'"([a-z_]+)"', block))


def test_form_layout_names_only_real_fields():
    """ROWS / EXCLUDED in add.html may only name `add` input fields."""
    html = (_STATIC / "add.html").read_text(encoding="utf-8")
    names = {f["name"] for f in add_field_schema()["fields"]}
    laid_out = _quoted_names(re.search(r"const ROWS = \[(.*?)\];", html, re.S).group(1))
    excluded = _quoted_names(re.search(r"const EXCLUDED = new Set\(\[(.*?)\]\)", html, re.S).group(1))
    assert laid_out <= names
    assert excluded <= names
    # Every field the form accepts is placed explicitly (unplaced ones would
    # still render on their own line, but should be laid out on purpose).
    assert names - excluded == laid_out


def test_schema_exposes_form_vocabularies():
    schema = add_field_schema()
    assert schema["formats"] and schema["remote_options"] and "AoE" in schema["timezones"]
    kinds = {f["name"]: f["kind"] for f in schema["fields"]}
    assert kinds["conference_dates"] == "dates"
    assert kinds["subcategory"] == "tags"
    assert kinds["attendance"] == "int"
    assert kinds["category"] == "tags"
    assert "medicine" in schema["categories"]
