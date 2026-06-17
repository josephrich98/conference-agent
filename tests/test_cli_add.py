"""Offline tests for the ``conference-agent add`` CLI subcommand.

Drive ``cli.main`` against a temporary SQLite file so the flag, CSV, merge,
overwrite, and category-warning paths are exercised end-to-end without network
access or an API key. The flags mirror the web table's columns.
"""

from datetime import date

from conference_agent.cli import main
from conference_agent.database import query_conferences


def _db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


def _by_id(url):
    """Return the stored conferences keyed by their id (upper-cased acronym)."""
    return {c.id: c for c in query_conferences(db_url=url)}


def test_add_new_conference_via_flags(tmp_path):
    url = _db_url(tmp_path)
    # A non-seed acronym so curated URL / category floors do not mask the input.
    code = main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - Test Imaging Conference",
            "--category",
            "radiology",
            "machine learning",
            "--location",
            "Chicago, IL",
            "--reputation",
            "big",
            "--remote-option",
            "hybrid",
            "--cost",
            "$500",
            "--url",
            "example.org/zzt",
            "--abstract-due",
            "2026-04-08",
            "--paper-due",
            "2026-05-13",
            "--conference-dates",
            "2026-11-29",
            "2026-12-03",
        ]
    )
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.acronym == "ZZT"
    assert conf.name == "Test Imaging Conference"
    assert conf.categories == ["radiology", "machine learning"]
    assert conf.location == "Chicago, IL"
    assert conf.reputation.value == "big"
    assert conf.remote_option.value == "hybrid"
    assert conf.cost == "$500"
    assert conf.url == "example.org/zzt"
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)
    assert conf.upcoming_paper_deadline == date(2026, 5, 13)
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_end_date == date(2026, 12, 3)
    # Month columns are derived from the dates, not supplied.
    assert conf.abstract_month == 4
    assert conf.paper_month == 5
    assert conf.conference_month == 11


def test_conference_dates_accepts_single_start(tmp_path):
    url = _db_url(tmp_path)
    code = main(
        ["--db", url, "add", "--conference", "ZZT - T", "--category", "radiology", "--conference-dates", "2026-11-29"]
    )
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_end_date is None


def test_conference_dates_rejects_more_than_two(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - T",
            "--category",
            "radiology",
            "--conference-dates",
            "2026-11-29",
            "2026-12-03",
            "2027-01-01",
        ]
    )
    assert code == 1
    assert "at most two dates" in capsys.readouterr().err


def test_bare_acronym_updates_existing_and_preserves_fields(tmp_path):
    url = _db_url(tmp_path)
    main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - Test Imaging Conference",
            "--category",
            "radiology",
            "--conference-dates",
            "2026-11-29",
        ]
    )
    # A bare acronym (no name) updates the existing row; other fields persist.
    code = main(["--db", url, "add", "--conference", "ZZT", "--abstract-due", "2026-04-08"])
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    assert conf.categories == ["radiology"]
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)


def test_conference_accepts_em_dash_separator(tmp_path):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference", "ZZT — Test Imaging Conference", "--category", "radiology"])
    assert code == 0
    assert _by_id(url)["ZZT"].name == "Test Imaging Conference"


def test_add_overwrite_clears_unsupplied_fields(tmp_path):
    url = _db_url(tmp_path)
    main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - Test Imaging Conference",
            "--category",
            "radiology",
            "--reputation",
            "big",
            "--conference-dates",
            "2026-11-29",
        ]
    )
    code = main(
        ["--db", url, "add", "--overwrite", "--conference", "ZZT - Test Imaging Conference", "--category", "radiology"]
    )
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    # Fields not supplied to --overwrite are cleared.
    assert conf.reputation is None
    assert conf.upcoming_start_date is None


def test_add_from_csv_inserts_multiple_rows(tmp_path):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    # Column names are the stored fields, so the web table's CSV export round-trips.
    csv_path.write_text(
        "acronym,name,category,upcoming_start_date,reputation,remote_option\n"
        "AAA,Conf A,radiology,2026-05-12,medium,in-person\n"
        "BBB,Conf B,genomics,2026-09-23,big,hybrid\n",
        encoding="utf-8",
    )
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 0
    stored = _by_id(url)
    assert set(stored) == {"AAA", "BBB"}
    assert stored["AAA"].upcoming_start_date == date(2026, 5, 12)
    assert stored["BBB"].reputation.value == "big"
    assert stored["BBB"].remote_option.value == "hybrid"


def test_add_from_csv_with_table_facing_columns(tmp_path):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    # The CSV header uses the same friendly column names as the flags: a
    # "conference" column (ACRONYM - Name) and a space-separated "conference_dates".
    csv_path.write_text(
        "conference,category,reputation,remote_option,abstract_due,conference_dates\n"
        'ZZT - Test Imaging Conference,"radiology, machine learning",big,hybrid,2026-04-08,2026-11-29 2026-12-03\n',
        encoding="utf-8",
    )
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    assert conf.categories == ["radiology", "machine learning"]
    assert conf.reputation.value == "big"
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_end_date == date(2026, 12, 3)


def test_add_csv_row_without_identity_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    csv_path.write_text("conference,category\n,radiology\n", encoding="utf-8")
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 1
    assert "no 'conference'" in capsys.readouterr().err


def test_add_requires_conference_without_csv(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--category", "radiology"])
    assert code == 1
    assert "--conference is required" in capsys.readouterr().err


def test_add_new_without_name_is_not_inserted(tmp_path, capsys):
    url = _db_url(tmp_path)
    # A bare acronym for a brand-new id has no name, so it cannot be inserted.
    code = main(["--db", url, "add", "--conference", "GHOST", "--conference-dates", "2027-01-01"])
    assert code == 0
    assert _by_id(url) == {}
    assert "require at least name and category" in capsys.readouterr().err


def test_add_overwrite_without_name_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--overwrite", "--conference", "ZZT", "--category", "radiology"])
    assert code == 1
    assert "cannot build conference" in capsys.readouterr().err
    assert _by_id(url) == {}


def test_add_rejects_invalid_enum_value(tmp_path):
    url = _db_url(tmp_path)
    # argparse `choices` rejects an out-of-vocabulary reputation before any write.
    try:
        main(["--db", url, "add", "--conference", "ZZT - X", "--category", "y", "--reputation", "huge"])
        raised = False
    except SystemExit as exc:
        raised = exc.code != 0
    assert raised


def test_add_warns_on_new_category_but_still_writes(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference", "ZZT - Test", "--category", "radiology", "quantum imaging"])
    assert code == 0
    err = capsys.readouterr().err
    # The unfamiliar tag is flagged; the known seed category is not.
    assert "quantum imaging" in err
    assert "radiology" not in err
    assert _by_id(url)["ZZT"].categories == ["radiology", "quantum imaging"]
