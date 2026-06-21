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
            "--subcategory",
            "radiology",
            "machine learning",
            "--location",
            "Chicago, IL",
            "--attendance",
            "45000",
            "--attendance-year",
            "2025",
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
    assert conf.subcategories == ["radiology", "machine learning"]
    assert conf.location == "Chicago, IL"
    assert conf.attendance == 45000
    assert conf.attendance_year == 2025
    assert conf.size.value == "large"  # derived from attendance
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


def test_add_formats_via_flag(tmp_path):
    url = _db_url(tmp_path)
    code = main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - Test Imaging Conference",
            "--subcategory",
            "radiology",
            "--format",
            "abstract",
            "poster",
            "oral",
        ]
    )
    assert code == 0
    # Stored in the canonical abstract/paper/poster/oral order.
    assert _by_id(url)["ZZT"].formats == ["abstract", "poster", "oral"]


def test_add_rejects_invalid_format_value(tmp_path):
    url = _db_url(tmp_path)
    # argparse `choices` rejects an out-of-vocabulary format before any write.
    try:
        main(["--db", url, "add", "--conference", "ZZT - X", "--subcategory", "y", "--format", "keynote"])
        raised = False
    except SystemExit as exc:
        raised = exc.code != 0
    assert raised


def test_add_formats_from_csv_column(tmp_path):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    csv_path.write_text(
        "conference,category,format\n"
        'ZZT - Test Imaging Conference,radiology,"poster, oral"\n',
        encoding="utf-8",
    )
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 0
    assert _by_id(url)["ZZT"].formats == ["poster", "oral"]


def test_conference_dates_accepts_single_start(tmp_path):
    url = _db_url(tmp_path)
    code = main(
        ["--db", url, "add", "--conference", "ZZT - T", "--subcategory", "radiology", "--conference-dates", "2026-11-29"]
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
            "--subcategory",
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
            "--subcategory",
            "radiology",
            "--conference-dates",
            "2026-11-29",
        ]
    )
    # A bare acronym (no name) updates the existing row; other fields persist.
    # --yes skips the "matches an existing entry" confirmation prompt.
    code = main(["--db", url, "add", "--yes", "--conference", "ZZT", "--abstract-due", "2026-04-08"])
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    assert conf.subcategories == ["radiology"]
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)


def test_conference_accepts_em_dash_separator(tmp_path):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference", "ZZT — Test Imaging Conference", "--subcategory", "radiology"])
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
            "--subcategory",
            "radiology",
            "--attendance",
            "45000",
            "--conference-dates",
            "2026-11-29",
        ]
    )
    code = main(
        ["--db", url, "add", "--yes", "--overwrite", "--conference", "ZZT - Test Imaging Conference", "--subcategory", "radiology"]
    )
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    # Fields not supplied to --overwrite are cleared (and size follows attendance).
    assert conf.attendance is None
    assert conf.size is None
    assert conf.upcoming_start_date is None


def test_add_from_csv_inserts_multiple_rows(tmp_path):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    # Column names are the stored fields, so the web table's CSV export round-trips.
    csv_path.write_text(
        "acronym,name,category,upcoming_start_date,attendance,remote_option\n"
        "AAA,Conf A,radiology,2026-05-12,500,in-person\n"
        "BBB,Conf B,genomics,2026-09-23,12000,hybrid\n",
        encoding="utf-8",
    )
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 0
    stored = _by_id(url)
    assert set(stored) == {"AAA", "BBB"}
    assert stored["AAA"].upcoming_start_date == date(2026, 5, 12)
    assert stored["AAA"].size.value == "medium"  # 500 attendees
    assert stored["BBB"].attendance == 12000
    assert stored["BBB"].size.value == "large"  # 12000 attendees
    assert stored["BBB"].remote_option.value == "hybrid"


def test_add_from_csv_with_table_facing_columns(tmp_path):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    # The CSV header uses the same friendly column names as the flags: a
    # "conference" column (ACRONYM - Name) and a space-separated "conference_dates".
    csv_path.write_text(
        "conference,category,attendance,remote_option,abstract_due,conference_dates\n"
        'ZZT - Test Imaging Conference,"radiology, machine learning",12000,hybrid,2026-04-08,2026-11-29 2026-12-03\n',
        encoding="utf-8",
    )
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 0
    conf = _by_id(url)["ZZT"]
    assert conf.name == "Test Imaging Conference"
    assert conf.subcategories == ["radiology", "machine learning"]
    assert conf.size.value == "large"  # derived from 12000 attendees
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
    code = main(["--db", url, "add", "--subcategory", "radiology"])
    assert code == 1
    assert "--conference is required" in capsys.readouterr().err


def test_add_new_without_name_is_not_inserted(tmp_path, capsys):
    url = _db_url(tmp_path)
    # A bare acronym for a brand-new id has no name, so it cannot be inserted.
    code = main(["--db", url, "add", "--conference", "GHOST", "--conference-dates", "2027-01-01"])
    assert code == 0
    assert _by_id(url) == {}
    assert "require at least name and a subcategory" in capsys.readouterr().err


def test_add_overwrite_without_name_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--overwrite", "--conference", "ZZT", "--subcategory", "radiology"])
    assert code == 1
    assert "cannot build conference" in capsys.readouterr().err
    assert _by_id(url) == {}


def test_add_rejects_invalid_enum_value(tmp_path):
    url = _db_url(tmp_path)
    # argparse `choices` rejects an out-of-vocabulary remote option before any write.
    try:
        main(["--db", url, "add", "--conference", "ZZT - X", "--subcategory", "y", "--remote-option", "telepathic"])
        raised = False
    except SystemExit as exc:
        raised = exc.code != 0
    assert raised


def test_add_warns_on_new_subcategory_but_still_writes(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference", "ZZT - Test", "--subcategory", "radiology", "quantum imaging"])
    assert code == 0
    err = capsys.readouterr().err
    # The unfamiliar tag is flagged; the known seed subcategory is not.
    assert "quantum imaging" in err
    assert "radiology" not in err
    assert _by_id(url)["ZZT"].subcategories == ["radiology", "quantum imaging"]


def _seed_zzt(url):
    main(
        [
            "--db",
            url,
            "add",
            "--conference",
            "ZZT - Test Imaging Conference",
            "--subcategory",
            "radiology",
            "--conference-dates",
            "2026-11-29",
        ]
    )


def test_add_matching_entry_prompts_and_accepts(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)
    # A bare acronym that matches the table is confirmed, so the update applies.
    code = main(["--db", url, "add", "--conference", "ZZT", "--abstract-due", "2026-04-08"])
    assert code == 0
    assert prompts and "already exists" in prompts[0]
    assert _by_id(url)["ZZT"].upcoming_abstract_deadline == date(2026, 4, 8)


def test_add_matching_entry_prompts_and_declines(tmp_path, monkeypatch, capsys):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    # Declining the prompt leaves the existing entry unchanged.
    code = main(["--db", url, "add", "--conference", "ZZT - Test Imaging Conference", "--abstract-due", "2026-04-08"])
    assert code == 0
    captured = capsys.readouterr()
    assert "left unchanged" in (captured.out + captured.err)
    assert _by_id(url)["ZZT"].upcoming_abstract_deadline is None


def test_add_yes_flag_skips_prompt(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    _seed_zzt(url)

    def boom(prompt=""):
        raise AssertionError("input() should not be called when --yes is passed")

    monkeypatch.setattr("builtins.input", boom)
    code = main(["--db", url, "add", "--yes", "--conference", "ZZT", "--abstract-due", "2026-04-08"])
    assert code == 0
    assert _by_id(url)["ZZT"].upcoming_abstract_deadline == date(2026, 4, 8)


def test_add_new_conference_not_prompted(tmp_path, monkeypatch):
    url = _db_url(tmp_path)

    def boom(prompt=""):
        raise AssertionError("a brand-new conference must not trigger the match prompt")

    monkeypatch.setattr("builtins.input", boom)
    code = main(["--db", url, "add", "--conference", "ZZT - Test Imaging Conference", "--subcategory", "radiology"])
    assert code == 0
    assert _by_id(url)["ZZT"].name == "Test Imaging Conference"
