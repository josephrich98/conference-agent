"""Offline tests for the ``conference-agent add`` / ``delete`` CLI subcommands.

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
    """Return the stored conferences keyed by their acronym (distinct in these tests).

    The real id is the slug of the name; keying by acronym keeps the tests short.
    """
    return {c.acronym: c for c in query_conferences(db_url=url)}


def test_add_new_conference_via_flags(tmp_path):
    url = _db_url(tmp_path)
    # A non-seed acronym so curated URL / category floors do not mask the input.
    code = main(
        [
            "--db",
            url,
            "add",
            "--conference-acronym",
            "ZZT",
            "--conference-name",
            "Test Imaging Conference",
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
    assert conf.size.value == "massive"  # derived from attendance
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


def test_add_late_abstract_and_prior_fields_via_flags(tmp_path):
    url = _db_url(tmp_path)
    # The CSHL Biological Data Science shape: a talk-only abstract deadline with
    # a later poster-only one, plus the prior edition's dates.
    code = main(
        [
            "--db", url, "add", "-y",
            "--conference", "ZZB - Biological Data Science",
            "--subcategory", "genomics",
            "--abstract-due", "2026-08-28",
            "--late-abstract-due", "2026-10-01",
            "--conference-dates", "2026-11-11", "2026-11-14",
            "--prior-abstract-due", "2025-08-29",
            "--prior-late-abstract-due", "2025-10-02",
            "--prior-conference-dates", "2025-11-12", "2025-11-15",
            "--notes", "Two abstract deadlines: talks, then posters.",
        ]
    )
    assert code == 0
    conf = _by_id(url)["ZZB"]
    assert conf.upcoming_abstract_deadline == date(2026, 8, 28)
    assert conf.upcoming_late_abstract_deadline == date(2026, 10, 1)
    assert conf.prior_abstract_deadline == date(2025, 8, 29)
    assert conf.prior_late_abstract_deadline == date(2025, 10, 2)
    assert conf.prior_start_date == date(2025, 11, 12)
    assert conf.prior_end_date == date(2025, 11, 15)
    assert conf.notes == "Two abstract deadlines: talks, then posters."
    # The month columns follow from the dates, with no separate input.
    assert (conf.abstract_month, conf.late_abstract_month) == (8, 10)


def test_add_name_only_keys_on_the_name(tmp_path):
    """A series with no acronym is stored with acronym == name (shown as the name)."""
    url = _db_url(tmp_path)
    code = main(
        ["--db", url, "add", "--conference-name", "ZZWeek", "--subcategory", "radiology"]
    )
    assert code == 0
    conf = _by_id(url)["ZZWeek"]
    assert conf.acronym == conf.name == "ZZWeek"
    assert conf.id == "zzweek"


def test_add_legacy_combined_conference_still_parses(tmp_path):
    """The old 'ACRONYM - Name' value is accepted; explicit fields win over it."""
    url = _db_url(tmp_path)
    code = main(
        [
            "--db", url, "add",
            "--conference", "ZZL - Legacy Name",
            "--conference-name", "Explicit Name",
            "--subcategory", "radiology",
        ]
    )
    assert code == 0
    conf = _by_id(url)["ZZL"]
    assert conf.name == "Explicit Name"


def test_add_from_json_file(tmp_path):
    url = _db_url(tmp_path)
    path = tmp_path / "records.json"
    path.write_text(
        '[{"conference_acronym": "ZZJ", "conference_name": "JSON Conference",'
        ' "subcategory": "genomics",'
        ' "abstract_due": "2026-04-09", "late_abstract_due": "2026-05-07",'
        ' "location": "Boston, MA"}]',
        encoding="utf-8",
    )
    assert main(["--db", url, "add", "-y", "--json", str(path)]) == 0
    conf = _by_id(url)["ZZJ"]
    assert conf.name == "JSON Conference"
    assert conf.upcoming_abstract_deadline == date(2026, 4, 9)
    assert conf.upcoming_late_abstract_deadline == date(2026, 5, 7)
    assert conf.location == "Boston, MA"


def test_add_json_accepts_a_single_object(tmp_path):
    url = _db_url(tmp_path)
    path = tmp_path / "one.json"
    path.write_text(
        '{"conference": "ZZO - One Object", "subcategory": "genomics"}',
        encoding="utf-8",
    )
    assert main(["--db", url, "add", "-y", "--json", str(path)]) == 0
    assert "ZZO" in _by_id(url)


def test_add_rejects_csv_and_json_together(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--csv", "a.csv", "--json", "b.json"])
    assert code == 1
    assert "either --csv or --json" in capsys.readouterr().err


def test_add_late_abstract_from_csv_column(tmp_path):
    url = _db_url(tmp_path)
    path = tmp_path / "confs.csv"
    path.write_text(
        "conference,subcategory,abstract_due,late_abstract_due\n"
        "ZZC - CSV Conference,genomics,2026-04-09,2026-05-07\n",
        encoding="utf-8",
    )
    assert main(["--db", url, "add", "-y", "--csv", str(path)]) == 0
    conf = _by_id(url)["ZZC"]
    assert conf.upcoming_abstract_deadline == date(2026, 4, 9)
    assert conf.upcoming_late_abstract_deadline == date(2026, 5, 7)


def test_fields_command_lists_every_add_flag(tmp_path):
    """`add --fields` is the input contract agents read — it must cover every flag."""

    from conference_agent.cli import _COMPOSITE_FIELDS, _SCALAR_FIELDS, build_parser

    parser = build_parser()
    add_parser = parser._subparsers._group_actions[0].choices["add"]
    flags = {
        opt
        for action in add_parser._actions
        for opt in action.option_strings
        if opt.startswith("--")
    }
    documented = {
        f"--{f.column.replace('_', '-')}"
        for f in list(_SCALAR_FIELDS) + list(_COMPOSITE_FIELDS)
    }
    # Every documented field is a real flag...
    assert documented <= flags
    # ...and every value-carrying flag is documented (bar the input/mode switches).
    # --conference is the hidden legacy "ACRONYM - Name" shorthand.
    modes = {
        "--csv", "--json", "--fields", "--update", "--delete", "--overwrite", "--yes",
        "--help", "--db", "--conference",
    }
    assert flags - modes == documented


def test_fields_json_is_machine_readable(capsys):
    import json as _json

    assert main(["add", "--fields", "json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    names = {f["name"] for f in payload["fields"]}
    assert {"conference_acronym", "conference_name", "abstract_due", "late_abstract_due"} <= names
    assert "conference" not in names
    late = next(f for f in payload["fields"] if f["name"] == "late_abstract_due")
    assert late["stored_as"] == "upcoming_late_abstract_deadline"
    assert late["flag"] == "--late-abstract-due"
    # The derived columns are listed as outputs, never as inputs.
    assert {d["name"] for d in payload["derived"]} & {"size", "category"}
    assert "size" not in names


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


def test_update_by_name_preserves_unsupplied_fields(tmp_path):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--update", "--conference-name", "test imaging  conference",
         "--abstract-due", "2026-04-08"]
    )
    assert code == 0
    conf = _by_id(url)["ZZT"]
    # The name matches case- and whitespace-insensitively; the stored spelling stays.
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
        ["--db", url, "add", "--update", "--overwrite", "--conference-name", "Test Imaging Conference", "--subcategory", "radiology"]
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
    assert stored["BBB"].size.value == "massive"  # 12000 attendees
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
    assert conf.size.value == "massive"  # derived from 12000 attendees
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert conf.upcoming_end_date == date(2026, 12, 3)


def test_add_csv_row_without_identity_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    csv_path = tmp_path / "confs.csv"
    csv_path.write_text("conference_acronym,category\nZZT,radiology\n", encoding="utf-8")
    code = main(["--db", url, "add", "--csv", str(csv_path)])
    assert code == 1
    assert "no 'conference_name'" in capsys.readouterr().err


def test_add_requires_conference_without_csv(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--subcategory", "radiology"])
    assert code == 1
    assert "--conference-name is required" in capsys.readouterr().err


def test_add_acronym_without_name_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference-acronym", "GHOST", "--subcategory", "radiology"])
    assert code == 1
    assert "--conference-name is required" in capsys.readouterr().err
    assert _by_id(url) == {}


def test_add_new_without_subcategory_errors(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--conference-name", "Ghost Conference"])
    assert code == 1
    assert "needs at least one --subcategory" in capsys.readouterr().err
    assert _by_id(url) == {}


def test_overwrite_requires_update(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--overwrite", "--conference-name", "X", "--subcategory", "radiology"])
    assert code == 1
    assert "--overwrite applies only with --update" in capsys.readouterr().err


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


def test_add_existing_name_fails(tmp_path, capsys):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--conference-name", "Test Imaging Conference",
         "--subcategory", "radiology", "--abstract-due", "2026-04-08"]
    )
    assert code == 1
    assert "already exists" in capsys.readouterr().err
    assert _by_id(url)["ZZT"].upcoming_abstract_deadline is None


def test_add_shared_acronym_with_a_new_name_is_a_new_series(tmp_path):
    """Entries are indexed by name: two series may share an acronym."""
    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--conference-acronym", "ZZT", "--conference-name", "Other",
         "--subcategory", "radiology"]
    )
    assert code == 0
    names = {c.id: c.name for c in query_conferences(db_url=url)}
    assert names == {"test-imaging-conference": "Test Imaging Conference", "other": "Other"}


def test_add_name_matching_ignores_case_and_punctuation(tmp_path, capsys):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--conference-name", "test-imaging CONFERENCE",
         "--subcategory", "radiology"]
    )
    assert code == 1
    assert "already exists" in capsys.readouterr().err


def test_add_batch_with_one_conflict_writes_nothing(tmp_path, capsys):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    path = tmp_path / "records.json"
    path.write_text(
        '[{"conference_acronym": "ZZN", "conference_name": "New One", "subcategory": "genomics"},'
        ' {"conference_name": "Test Imaging Conference", "subcategory": "radiology"}]',
        encoding="utf-8",
    )
    assert main(["--db", url, "add", "--json", str(path)]) == 1
    assert set(_by_id(url)) == {"ZZT"}


def test_update_missing_name_fails(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "add", "--update", "--conference-name", "Nope", "--abstract-due", "2026-04-08"])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_update_can_rename_acronym(tmp_path):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--update", "--conference-name", "Test Imaging Conference",
         "--conference-acronym", "ZZX"]
    )
    assert code == 0
    [conf] = query_conferences(db_url=url)
    assert conf.acronym == "ZZX"
    assert conf.id == "test-imaging-conference"  # the id follows the name only
    assert conf.upcoming_start_date == date(2026, 11, 29)


def test_update_new_conference_name_renames_and_keeps_old_id_resolving(tmp_path):
    from conference_agent.database import former_ids, resolve_ids

    url = _db_url(tmp_path)
    _seed_zzt(url)
    code = main(
        ["--db", url, "add", "--update", "--conference-name", "Test Imaging Conference",
         "--new-conference-name", "Renamed Imaging Conference", "--abstract-due", "2026-04-08"]
    )
    assert code == 0
    [conf] = query_conferences(db_url=url)
    assert (conf.id, conf.name) == ("renamed-imaging-conference", "Renamed Imaging Conference")
    assert conf.upcoming_abstract_deadline == date(2026, 4, 8)
    assert conf.upcoming_start_date == date(2026, 11, 29)
    assert former_ids(url) == {"test-imaging-conference": "renamed-imaging-conference"}
    assert resolve_ids(["test-imaging-conference"], url) == {
        "test-imaging-conference": "renamed-imaging-conference"
    }

    # The old name is free again: adding it makes a new series, not an update.
    code = main(
        ["--db", url, "add", "--conference-name", "Test Imaging Conference",
         "--subcategory", "radiology"]
    )
    assert code == 0
    assert len(query_conferences(db_url=url)) == 2
    assert former_ids(url) == {}


def test_update_rename_onto_an_existing_name_fails(tmp_path, capsys):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    main(["--db", url, "add", "--conference-name", "Other", "--subcategory", "radiology"])
    code = main(
        ["--db", url, "add", "--update", "--conference-name", "Test Imaging Conference",
         "--new-conference-name", "other"]
    )
    assert code == 1
    assert "already in use" in capsys.readouterr().err
    assert {c.name for c in query_conferences(db_url=url)} == {"Test Imaging Conference", "Other"}


def test_new_conference_name_without_update_fails(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(
        ["--db", url, "add", "--conference-name", "X", "--subcategory", "radiology",
         "--new-conference-name", "Y"]
    )
    assert code == 1
    assert "applies only with --update" in capsys.readouterr().err


def test_delete_subcommand_with_yes(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    _seed_zzt(url)

    def boom(prompt=""):
        raise AssertionError("input() should not be called when --yes is passed")

    monkeypatch.setattr("builtins.input", boom)
    code = main(["--db", url, "delete", "--yes", "--conference-name", "Test Imaging Conference"])
    assert code == 0
    assert _by_id(url) == {}


def test_add_delete_prompts_and_declines(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code = main(["--db", url, "add", "--delete", "--conference-name", "Test Imaging Conference"])
    assert code == 1
    assert set(_by_id(url)) == {"ZZT"}


def test_add_delete_prompts_and_accepts(tmp_path, monkeypatch):
    url = _db_url(tmp_path)
    _seed_zzt(url)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    code = main(["--db", url, "add", "--delete", "--conference-name", "Test Imaging Conference"])
    assert code == 0
    assert _by_id(url) == {}


def test_delete_missing_name_fails(tmp_path, capsys):
    url = _db_url(tmp_path)
    code = main(["--db", url, "delete", "--yes", "--conference-name", "Nope"])
    assert code == 1
    assert "does not exist" in capsys.readouterr().err


def test_add_new_conference_not_prompted(tmp_path, monkeypatch):
    url = _db_url(tmp_path)

    def boom(prompt=""):
        raise AssertionError("adding a brand-new conference must not prompt")

    monkeypatch.setattr("builtins.input", boom)
    code = main(["--db", url, "add", "--conference", "ZZT - Test Imaging Conference", "--subcategory", "radiology"])
    assert code == 0
    assert _by_id(url)["ZZT"].name == "Test Imaging Conference"


def test_fields_text_flags_columns_hidden_on_website(capsys):
    from conference_agent.cli import _COMPOSITE_FIELDS, _NOT_DISPLAYED, _SCALAR_FIELDS

    columns = {f.column for f in list(_SCALAR_FIELDS) + list(_COMPOSITE_FIELDS)}
    assert set(_NOT_DISPLAYED) <= columns

    assert main(["add", "--fields"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    i = lines.index("  attendance_year")
    assert "Not displayed on website — integrated into Attendance" in lines[i + 2]
    # A field with its own column carries no note.
    j = lines.index("  location")
    assert lines[j + 2] == ""


def _add(url, *extra):
    return main(["--db", url, "add", "--conference-name", "Value Check Meeting",
                 "--subcategory", "radiology", *extra])


def test_add_rejects_malformed_url(tmp_path, capsys):
    url = _db_url(tmp_path)
    assert _add(url, "--url", "notaurl") == 1
    assert _add(url, "--attendance-source", "ftp://example.org") == 1
    err = capsys.readouterr().err
    assert "url 'notaurl' is not an http(s) URL" in err
    assert "Nothing was written." in err
    assert query_conferences(db_url=url) == []
    assert _add(url, "--url", "www.example .org") == 1
    assert _add(url, "--url", "https://www.example.org/meeting") == 0
    # A bare domain is accepted (the table adds the scheme when linking).
    code = main(["--db", url, "add", "--conference-name", "Bare Domain Meeting",
                 "--subcategory", "radiology", "--url", "example.org/meeting"])
    assert code == 0


def test_add_rejects_end_date_before_start(tmp_path, capsys):
    url = _db_url(tmp_path)
    assert _add(url, "--conference-dates", "2026-05-10", "2026-05-01") == 1
    assert _add(url, "--prior-conference-dates", "2025-05-10", "2025-05-01") == 1
    assert "ends (2026-05-01) before it starts (2026-05-10)" in capsys.readouterr().err
    assert query_conferences(db_url=url) == []
    assert _add(url, "--conference-dates", "2026-05-01", "2026-05-01") == 0


def test_update_checks_date_order_against_stored_dates(tmp_path, capsys):
    url = _db_url(tmp_path)
    assert _add(url, "--conference-dates", "2026-05-01", "2026-05-05") == 0
    code = main(["--db", url, "add", "--update", "--conference-name", "Value Check Meeting",
                 "--conference-dates", "2026-05-09"])
    assert code == 1
    assert "before it starts" in capsys.readouterr().err
    stored = query_conferences(db_url=url)[0]
    assert stored.upcoming_start_date == date(2026, 5, 1)


def test_add_rejects_out_of_range_numbers(tmp_path, capsys):
    url = _db_url(tmp_path)
    assert _add(url, "--attendance", "-5") == 1
    assert _add(url, "--attendance", "0") == 1
    assert _add(url, "--attendance-year", "3") == 1
    assert _add(url, "--attendance-year", str(date.today().year + 1)) == 1
    err = capsys.readouterr().err
    assert "attendance -5 must be at least 1" in err
    assert "attendance_year 3 must be between 1900" in err
    assert query_conferences(db_url=url) == []
    assert _add(url, "--attendance", "450", "--attendance-year", "2025") == 0


def test_add_csv_rejects_non_numeric_attendance(tmp_path, capsys):
    url = _db_url(tmp_path)
    path = tmp_path / "rows.csv"
    path.write_text("conference_name,subcategory,attendance\nValue Check Meeting,radiology,lots\n")
    assert main(["--db", url, "add", "--csv", str(path)]) == 1
    assert "attendance 'lots' is not a whole number" in capsys.readouterr().err
