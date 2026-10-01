"""Deadline extension history entered through `add` (and the website form).

The form submits an `add --json` record whose ``deadline_extensions`` key holds
``{type, original, extended}`` objects; `add` appends them to the stored
history (``*_deadline_extension``) in the same ``MM/DD/YYYY – MM/DD/YYYY`` form
the automatic detection writes.
"""

import json
from datetime import date

import pytest
from sqlalchemy.orm import Session

from conference_agent.cli import add_field_schema, main
from conference_agent.database import (
    ConferenceRow,
    get_engine,
    parse_deadline_extensions,
)

_THIS_YEAR = date.today().year


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _row(url, acronym):
    with Session(get_engine(url)) as session:
        row = session.query(ConferenceRow).filter_by(acronym=acronym).one()
        session.expunge(row)
        return row


def test_parse_accepts_objects_triples_and_text():
    expected = [
        ("abstract", date(2026, 3, 1), date(2026, 3, 8)),
        ("late_abstract", date(2026, 5, 1), date(2026, 5, 15)),
    ]
    objects = [
        {"type": "abstract", "original": "2026-03-01", "extended": "2026-03-08"},
        {"type": "late abstract", "original": "2026-05-01", "extended": "2026-05-15"},
    ]
    assert parse_deadline_extensions(objects) == expected
    assert parse_deadline_extensions(
        [["abstract", "2026-03-01", "2026-03-08"], ["late_abstract", "2026-05-01", "2026-05-15"]]
    ) == expected
    assert parse_deadline_extensions(
        "abstract 2026-03-01 2026-03-08; late abstract 2026-05-01 2026-05-15"
    ) == expected
    assert parse_deadline_extensions(None) == []


@pytest.mark.parametrize(
    "value",
    [
        [{"type": "poster", "original": "2026-03-01", "extended": "2026-03-08"}],
        [{"type": "paper", "original": "2026-03-08", "extended": "2026-03-01"}],
        [{"type": "paper", "original": "03/01/2026", "extended": "2026-03-08"}],
        [{"type": "paper", "original": "2026-03-01"}],
    ],
)
def test_parse_rejects_malformed_entries(value):
    with pytest.raises(ValueError):
        parse_deadline_extensions(value)


def test_add_json_records_extension_history(tmp_path):
    url = _db_url(tmp_path)
    y = _THIS_YEAR
    path = tmp_path / "rec.json"
    path.write_text(
        json.dumps(
            {
                "conference_acronym": "ZZX",
                "conference_name": "Extension Test Conference",
                "subcategory": ["genomics"],
                "deadline_extensions": [
                    {"type": "abstract", "original": f"{y}-03-01", "extended": f"{y}-03-08"},
                    {"type": "abstract", "original": f"{y - 1}-03-02", "extended": f"{y - 1}-03-09"},
                    {"type": "paper", "original": f"{y}-04-01", "extended": f"{y}-04-15"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["--db", url, "add", "--json", str(path)]) == 0
    row = _row(url, "ZZX")
    assert row.abstract_deadline_extension == (
        f"03/01/{y} – 03/08/{y}\n03/02/{y - 1} – 03/09/{y - 1}"
    )
    assert row.paper_deadline_extension == f"04/01/{y} – 04/15/{y}"
    assert row.late_abstract_deadline_extension is None

    # An update appends to the history and skips an entry already on file.
    assert main(
        [
            "--db", url, "add", "--update", "--conference-name", "Extension Test Conference",
            "--deadline-extensions", "paper", f"{y}-04-01", f"{y}-04-15",
            "--deadline-extensions", "late_abstract", f"{y}-05-01", f"{y}-05-10",
        ]
    ) == 0
    row = _row(url, "ZZX")
    assert row.paper_deadline_extension == f"04/01/{y} – 04/15/{y}"
    assert row.late_abstract_deadline_extension == f"05/01/{y} – 05/10/{y}"


def test_add_rejects_entries_older_than_retention(tmp_path, capsys):
    url = _db_url(tmp_path)
    old = _THIS_YEAR - 6
    code = main(
        [
            "--db", url, "add", "--conference-name", "Old Extension Conference",
            "--subcategory", "genomics",
            "--deadline-extensions", "abstract", f"{old}-03-01", f"{old}-03-08",
        ]
    )
    assert code == 1
    assert "more than 5 years old" in capsys.readouterr().err


def test_schema_exposes_extension_field():
    kinds = {f["name"]: f["kind"] for f in add_field_schema()["fields"]}
    assert kinds["deadline_extensions"] == "extensions"
