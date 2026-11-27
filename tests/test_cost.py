"""The tiered cost format and the amount-based comparison that keeps rewordings
from rewriting the stored text or emailing subscribers."""

from datetime import date

from conference_agent.database import cost_to_store, get_engine, merge_records, upsert_conferences
from conference_agent.discover import _ExtractedCost
from conference_agent.models import (
    Conference,
    cost_amounts,
    is_structured_cost,
    normalize_cost,
    parse_cost,
    same_cost,
)
from conference_agent.subscriptions import diff_snapshots

TIERED = "Early bird: $865 member / $1,175 non-member\nRegular: $1,015 member / $1,365 non-member\nTrainee: $100"


def test_normalize_cost_canonicalizes_labels_amounts_and_order():
    text = (
        "students: EUR 90-120; Regular: 950 EUR Members / 1420EUR nonmembers\n"
        "early-bird: EUR 870 member / EUR 1,360 Non-Member\nonline: free"
    )
    assert normalize_cost(text) == (
        "Early bird: €870 member / €1,360 non-member\n"
        "Regular: €950 member / €1,420 non-member\n"
        "Trainee: €90–€120\n"
        "Virtual: free"
    )
    assert normalize_cost({"regular": "USD 500", "late": "", "notes": "Free for residents."}) == (
        "Regular: $500\nNotes: Free for residents."
    )
    assert normalize_cost(" \n ") is None
    assert normalize_cost(None) is None


def test_free_text_is_kept_as_given():
    legacy = "Early bird: $1,049 member / $1,199 non-member; early career $379 member"
    assert parse_cost(legacy) is None
    assert normalize_cost(legacy) == legacy
    assert not is_structured_cost(legacy)
    assert is_structured_cost(TIERED)


def test_cost_amounts_reads_currency_spellings_and_ranges():
    assert cost_amounts("CAD $1,200 / A$2,035 / 500 USD / €80-€400 / Free") == [
        ("", 0.0), ("$", 500.0), ("A$", 2035.0), ("CA$", 1200.0), ("€", 80.0), ("€", 400.0),
    ]


def test_same_cost_ignores_wording_but_not_amounts_or_tiers():
    reworded = "Early bird: $865 members, $1,175 for nonmembers\nRegular: Members $1,015 / non-members $1,365\nStudents: $100"
    assert same_cost(TIERED, reworded)
    assert not same_cost(TIERED, TIERED.replace("1,365", "1,395"))
    # The same amount moved to another tier is a change.
    assert not same_cost(TIERED, TIERED.replace("Trainee", "Virtual"))
    # Free text: compared by its amounts.
    assert same_cost("Member $865 early / $1,015 regular", "Members: $865 (early bird), $1,015 (regular)")
    # Tiers restating free text (dropping the industry rate) are the same cost.
    assert same_cost(TIERED, "Members $865 / $1,015; nonmembers $1,175 / $1,365; trainees $100; industry $2,000")
    assert not same_cost(TIERED, None)
    assert same_cost(None, "")


def test_cost_to_store_keeps_stored_text_unless_upgrading():
    reworded = TIERED.replace("Trainee", "Students")
    assert cost_to_store(TIERED, normalize_cost(reworded)) == TIERED
    legacy = "Members $865 / $1,015; nonmembers $1,175 / $1,365; trainees $100"
    assert cost_to_store(legacy, TIERED) == TIERED  # tiers replace free text
    assert cost_to_store(TIERED, legacy) == TIERED  # free text never replaces tiers
    changed = TIERED.replace("$100", "$150")
    assert cost_to_store(TIERED, changed) == changed


def test_subscriber_diff_ignores_reworded_cost():
    old = {"cost": "Members $865 early / $1,015 regular"}
    assert diff_snapshots(old, {"cost": "Early bird: $865 member\nRegular: $1,015 member"}) == []
    assert diff_snapshots(old, {"cost": "Early bird: $900 member\nRegular: $1,015 member"})


def test_extracted_cost_tiers_become_lines():
    item = _ExtractedCost(early_bird="EUR 710", trainee="EUR 90", notes="")
    assert normalize_cost(item.model_dump()) == "Early bird: €710\nTrainee: €90"


def _conf(**kw):
    base = dict(
        acronym="TC", name="Test Conference", subcategories=["radiology"],
        upcoming_start_date=date(2027, 5, 1), upcoming_end_date=date(2027, 5, 3),
    )
    base.update(kw)
    return Conference(**base)


def test_writes_keep_stored_cost_when_only_reworded(tmp_path):
    from sqlalchemy.orm import Session

    from conference_agent.database import ConferenceRow

    url = f"sqlite:///{tmp_path / 'c.db'}"
    get_engine(url)
    upsert_conferences([_conf(upcoming_cost=TIERED)], db_url=url)
    reworded = TIERED.replace("Trainee", "Students").replace("member /", "members /")
    upsert_conferences([_conf(upcoming_cost=reworded)], db_url=url)
    # A survey that reports no cost for the same edition keeps the stored one.
    upsert_conferences([_conf()], db_url=url)
    merge_records([{"id": "test-conference", "upcoming_cost": reworded}], db_url=url)
    with Session(get_engine(url)) as s:
        assert s.get(ConferenceRow, "test-conference").upcoming_cost == TIERED
    merge_records([{"id": "test-conference", "upcoming_cost": "Regular: $2,000"}], db_url=url)
    with Session(get_engine(url)) as s:
        assert s.get(ConferenceRow, "test-conference").upcoming_cost == "Regular: $2,000"
