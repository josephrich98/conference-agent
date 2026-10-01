"""Typed schema for a conference.

One ``Conference`` record describes a recurring conference *series* (e.g. RSNA),
holding both its most recent **prior** edition and its **upcoming** edition.
Series are indexed by their full name -- two series may share an acronym -- and
the record id is a URL-safe slug of it (:func:`name_id`), so re-running discovery
updates the same row each cycle: as a new edition is announced, today's
"upcoming" rolls into "prior" and the freshly announced dates become "upcoming".

Keeping prior and upcoming side by side lets the table show last year's dates as
a reference even before an organizer has published next year's schedule, which is
common many months out.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from conference_agent.deadline_time import (
    KINDS,
    format_deadline_time,
    normalize_time,
    normalize_timezone,
    parse_legacy_deadline_time,
)


class ConferenceSize(str, Enum):
    """Size bucket of a conference, an objective proxy for prominence.

    A controlled value (rather than free text) so table views and queries can
    filter and color consistently. Unlike a subjective reputation judgment, the
    bucket is *derived deterministically* from a sourced attendance figure (see
    :func:`size_for_attendance`), so it is a fact that can be cited rather than an
    opinion. Example: RSNA (~45,000 attendees) is ``massive``.
    """

    MASSIVE = "massive"
    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


# Attendance thresholds (inclusive lower bounds) that bucket a conference into a
# size. Centralized so the rule is changed in one place and applied identically
# on read (the ``Conference.size`` property) and on write (the stored ``size``
# column). A meeting with >= MASSIVE attendees is "massive"; >= LARGE is "large";
# >= MEDIUM is "medium"; fewer is "small"; an unknown attendance yields no size.
MASSIVE_ATTENDANCE_THRESHOLD = 10_000
LARGE_ATTENDANCE_THRESHOLD = 1_000
MEDIUM_ATTENDANCE_THRESHOLD = 250


def size_for_attendance(attendance: "int | None") -> "ConferenceSize | None":
    """Bucket an attendance figure into a :class:`ConferenceSize`.

    The single, deterministic size rule: ``None`` in, ``None`` out (size is left
    blank when attendance is unknown rather than guessed).
    """
    if attendance is None:
        return None
    if attendance >= MASSIVE_ATTENDANCE_THRESHOLD:
        return ConferenceSize.MASSIVE
    if attendance >= LARGE_ATTENDANCE_THRESHOLD:
        return ConferenceSize.LARGE
    if attendance >= MEDIUM_ATTENDANCE_THRESHOLD:
        return ConferenceSize.MEDIUM
    return ConferenceSize.SMALL


class RemoteOption(str, Enum):
    """Whether a conference can be attended remotely (``None`` when not known)."""

    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"


# Subcategories are stored as a single column (a comma-joined string) but modeled
# as a list, since one conference can belong to several fields (e.g. SPR is both
# radiology and pediatrics; MICCAI is radiology and machine learning). This helper
# tokenizes any accepted form -- a list, or a delimited string -- into a clean,
# lowercased, de-duplicated list, so the model, the database, and the refresh
# policy all split tags the same way.
def _split_tags(value: "str | list | tuple | None") -> List[str]:
    """Tokenize a list/tuple or ``,``/``;``-delimited string into clean tags.

    Lowercased, stripped, de-duplicated, and order-preserving. Shared by the
    subcategory and format normalizers so every tag column splits identically.
    """
    if value is None:
        parts: List[str] = []
    elif isinstance(value, str):
        parts = re.split(r"[;,]", value)
    else:
        parts = [p for item in value for p in re.split(r"[;,]", str(item))]
    seen: set[str] = set()
    out: List[str] = []
    for part in parts:
        tag = part.strip().lower()
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def normalize_subcategories(value: "str | list | tuple | None") -> List[str]:
    """Normalize subcategory tags to a lowercased, de-duplicated, ordered list."""
    return _split_tags(value)


# The ten suggested top-level categories. A conference's category is an input
# like its subcategories (multi-valued, one or more per series); these are the
# canonical values offered in pickers and listed first when present, but any
# other category is accepted as entered.
CATEGORIES = (
    "humanities",
    "social science",
    "medicine",
    "biology",
    "chemistry",
    "physics",
    "mathematics",
    "stats",
    "computer science",
    "artificial intelligence",
)


def normalize_categories(value: "str | list | tuple | None") -> List[str]:
    """Normalize category tags: lowercased and de-duplicated, with the
    :data:`CATEGORIES` values first in canonical order, then any custom ones in
    the order given."""
    tags = _split_tags(value)
    return [c for c in CATEGORIES if c in tags] + [t for t in tags if t not in CATEGORIES]


# The submission / presentation formats a conference offers. Unlike the free-text
# subcategory tags, this is a small controlled vocabulary: a meeting may invite a
# short abstract, a full paper / manuscript, a poster presentation, and/or an oral
# (podium) presentation -- often several at once. Listed here in the canonical
# display order so the column reads consistently regardless of input order.
CONFERENCE_FORMATS = ("abstract", "paper", "poster", "oral")


def normalize_formats(value: "str | list | tuple | None") -> List[str]:
    """Normalize submission/presentation formats to a canonical-ordered list.

    Accepts a list or a delimited string, lowercases and de-duplicates the tokens
    (reusing :func:`_split_tags`), then keeps only the recognized formats in
    :data:`CONFERENCE_FORMATS` and returns them in that canonical order. Tokens
    outside the vocabulary are dropped, so the column stays clean.
    """
    present = set(_split_tags(value))
    return [fmt for fmt in CONFERENCE_FORMATS if fmt in present]


def name_id(name: str) -> str:
    """The record id for a conference name: a lowercase, hyphenated ASCII slug.

    Series are indexed by name, so this is also the name comparison used to match
    rows: case, accents, punctuation, and spacing do not distinguish two names
    (``"IDWeek"`` and ``"idweek"`` are one series). The slug is the id in
    calendar event UIDs, subscription keys, and the ``/c/<id>/`` page URLs.
    """
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


# Per-edition fields, as the suffix after ``prior_`` / ``upcoming_``.
EDITION_SUFFIXES = (
    "abstract_deadline",
    "late_abstract_deadline",
    "paper_deadline",
    "start_date",
    "end_date",
    "registration",
    "location",
    "cost",
)


class Conference(BaseModel):
    """A recurring conference series with its prior and upcoming editions."""

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _collapse_duplicate_edition(cls, data):
        """Merge a prior edition that duplicates the upcoming one.

        Two editions cannot start on the same day, so a record whose prior and
        upcoming start dates are equal holds one edition twice (an extraction
        error). The prior slots fill any upcoming blanks and are then cleared;
        :func:`database.roll_past_editions` later moves the edition into the prior
        slots once it is over.
        """
        if not isinstance(data, dict):
            return data
        prior, upcoming = data.get("prior_start_date"), data.get("upcoming_start_date")
        if prior in (None, "") or str(prior) != str(upcoming):
            return data
        data = dict(data)
        for edition in ("prior", "upcoming"):  # fold the legacy alias in first
            alias = data.pop(f"{edition}_registration_date", None)
            data.setdefault(f"{edition}_registration", alias)
        for suffix in ("location", "cost"):  # likewise the single-value keys
            if data.get(f"upcoming_{suffix}") in (None, ""):
                data[f"upcoming_{suffix}"] = data.pop(suffix, None)
        for suffix in EDITION_SUFFIXES:
            value = data.pop(f"prior_{suffix}", None)
            if data.get(f"upcoming_{suffix}") in (None, "") and value not in (None, ""):
                data[f"upcoming_{suffix}"] = value
        return data

    # --- Identity ----------------------------------------------------------
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name (the series' index)")

    @field_validator("name")
    @classmethod
    def _name_has_id(cls, value: str) -> str:
        value = " ".join(value.split())
        if not name_id(value):
            raise ValueError("name must contain at least one ASCII letter or digit")
        return value
    # One conference can carry several subcategory tags (e.g. SPR -> radiology +
    # pediatrics). Accepts either a list or a comma/semicolon-delimited string (and
    # the singular ``subcategory`` key) on input; ``subcategory`` below exposes the
    # joined string for display and storage.
    subcategories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subcategories", "subcategory"),
        description="Specific field(s), e.g. ['radiology', 'machine learning']",
    )
    # The broad bucket(s), set independently of the subcategories: usually from
    # :data:`CATEGORIES` (e.g. MICCAI -> medicine + artificial intelligence), but
    # a custom category is kept as entered. ``category`` joins them.
    categories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("categories", "category"),
        description="Broad categories, e.g. ['medicine', 'artificial intelligence']",
    )

    @field_validator("subcategories", mode="before")
    @classmethod
    def _normalize_subcategories(cls, value):
        return normalize_subcategories(value)

    @field_validator("categories", mode="before")
    @classmethod
    def _normalize_categories(cls, value):
        return normalize_categories(value)

    # --- Prior (most recent completed) edition -----------------------------
    prior_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the most recent edition"
    )
    prior_late_abstract_deadline: Optional[date] = Field(
        None,
        description=(
            "Second, later abstract deadline of the most recent edition -- a "
            "poster-only or late-breaking window (see "
            ":attr:`upcoming_late_abstract_deadline`). Blank when the edition "
            "published only one abstract deadline."
        ),
    )
    prior_paper_deadline: Optional[date] = Field(
        None, description="Full paper / manuscript deadline of the most recent edition"
    )
    prior_start_date: Optional[date] = Field(
        None, description="First day of the most recent edition"
    )
    prior_end_date: Optional[date] = Field(
        None, description="Last day of the most recent edition"
    )
    prior_registration: Optional[str] = Field(
        None,
        description=(
            "Free-text registration window(s) of the most recent edition, e.g. "
            "'Early bird: Jan 5 - Mar 1; Regular: Mar 2 - conference' or "
            "'Registration opens June 2025'. Blank when no info is available."
        ),
        validation_alias=AliasChoices("prior_registration", "prior_registration_date"),
    )
    prior_location: Optional[str] = Field(
        None, description="Host city / venue of the most recent edition, e.g. 'Chicago, IL'"
    )
    prior_cost: Optional[str] = Field(
        None, description="Registration cost summary of the most recent edition"
    )

    # --- Upcoming edition --------------------------------------------------
    upcoming_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the upcoming edition"
    )
    upcoming_late_abstract_deadline: Optional[date] = Field(
        None,
        description=(
            "Second, later abstract deadline of the upcoming edition. Many series "
            "publish two: a main deadline and a later window that is narrower in "
            "scope. Two shapes recur -- a poster-only deadline when the main one "
            "is talk-only (CSHL Biological Data Science: talks Aug 28, posters "
            "Oct 1), and a late-breaking / late-poster round after the main call "
            "closes (ASHG, ISMB, RECOMB). Both go here; "
            "``upcoming_abstract_deadline`` always holds the earlier, primary "
            "deadline. Blank when the edition publishes only one."
        ),
    )
    upcoming_paper_deadline: Optional[date] = Field(
        None, description="Full paper / manuscript deadline of the upcoming edition"
    )
    upcoming_start_date: Optional[date] = Field(
        None, description="First day of the upcoming edition"
    )
    upcoming_end_date: Optional[date] = Field(
        None, description="Last day of the upcoming edition"
    )
    upcoming_registration: Optional[str] = Field(
        None,
        description=(
            "Free-text registration window(s) of the upcoming edition, e.g. "
            "'Early bird: Jan 5 - Mar 1; Regular: Mar 2 - conference' or "
            "'Registration opens June 2026'. Blank when no info is available."
        ),
        validation_alias=AliasChoices("upcoming_registration", "upcoming_registration_date"),
    )
    # Location and cost belong to an edition (a series moves cities and changes
    # its fees). The legacy single ``location`` / ``cost`` input keys fill the
    # upcoming slot; the :attr:`location` / :attr:`cost` properties give the
    # displayed value (upcoming, else prior).
    upcoming_location: Optional[str] = Field(
        None,
        description="Host city / venue of the upcoming edition, e.g. 'Chicago, IL' or 'Vienna, Austria'",
        validation_alias=AliasChoices("upcoming_location", "location"),
    )
    upcoming_cost: Optional[str] = Field(
        None,
        description="Registration cost summary of the upcoming edition, e.g. '$1,095 (member, early-bird)'",
        validation_alias=AliasChoices("upcoming_cost", "cost"),
    )

    # --- Logistics & classification ----------------------------------------
    # True when the series meets in the same place every edition (RSNA is always
    # in Chicago), so a location recorded for a prior edition still describes the
    # next one and the table shows it as current rather than as a past value.
    stable_location: bool = Field(
        False, description="Whether the series is held in the same location every edition"
    )
    url: Optional[str] = Field(None, description="Official conference website link")
    remote_option: Optional[RemoteOption] = Field(
        None, description="In-person / virtual / hybrid attendance option"
    )
    # Submission / presentation formats the conference offers (any of abstract,
    # paper, poster, oral). Like categories, accepts either a list or a
    # comma/semicolon-delimited string (and the singular ``format`` key) on input;
    # ``format`` below exposes the joined string for display and storage. Distinct
    # from ``remote_option`` (how you attend) -- this is how work is presented.
    formats: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("formats", "format"),
        description="Submission/presentation format(s) offered, any of: abstract, paper, poster, oral",
    )

    @field_validator("stable_location", mode="before")
    @classmethod
    def _parse_stable_location(cls, value):
        # Blank means "not stated", i.e. False; accept yes/no spellings from CSV.
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "yes", "y", "1"):
                return True
            if text in ("false", "no", "n", "0"):
                return False
        return value

    @field_validator("remote_option", mode="before")
    @classmethod
    def _blank_unknown_remote(cls, value):
        # An unknown option is stored as no value, not as its own category.
        if isinstance(value, str) and value.strip().lower() in ("", "unknown"):
            return None
        return value

    @field_validator("formats", mode="before")
    @classmethod
    def _normalize_formats(cls, value):
        return normalize_formats(value)
    # Attendance is the objective input from which ``size`` is derived. The figure
    # is paired with the year it describes and the source it was taken from, so the
    # derived size is auditable rather than a bare assertion. The source URL is
    # provenance kept internal (it is not surfaced in the public table).
    attendance: Optional[int] = Field(
        None, description="Typical annual attendee count, e.g. 45000"
    )
    attendance_year: Optional[int] = Field(
        None, description="Year the attendance figure describes, e.g. 2025"
    )
    attendance_source: Optional[str] = Field(
        None, description="Source URL the attendance figure was taken from (internal provenance)"
    )
    notes: Optional[str] = Field(None, description="Free-form notes")
    # Time of day and time zone each deadline closes, one pair per deadline kind.
    # These are the stored source of truth (not shown as columns); the table's
    # ``Deadline time`` text is *derived* from them (see :attr:`deadline_time`).
    # Times are canonical 24-hour ``HH:MM`` and zones are canonical codes
    # (``AoE``, ``ET``, ``CET``, ... or an IANA name), whatever form the input took
    # -- see ``deadline_time.py``. The deadline columns stay pure dates (sorting,
    # derived months, date comparisons, and all-day calendar events depend on
    # that), so a time rides along per kind. A zone with no time, or a time with no
    # zone, is kept as published.
    abstract_time: Optional[str] = Field(None, description="Abstract deadline time of day, 24-hour HH:MM")
    abstract_timezone: Optional[str] = Field(None, description="Zone of the abstract deadline time, e.g. 'AoE', 'ET'")
    late_abstract_time: Optional[str] = Field(None, description="Late abstract deadline time of day, 24-hour HH:MM")
    late_abstract_timezone: Optional[str] = Field(None, description="Zone of the late abstract deadline time")
    paper_time: Optional[str] = Field(None, description="Paper deadline time of day, 24-hour HH:MM")
    paper_timezone: Optional[str] = Field(None, description="Zone of the paper deadline time")

    @model_validator(mode="before")
    @classmethod
    def _structure_deadline_times(cls, data):
        """Accept the legacy free-text ``deadline_time`` as input.

        ``deadline_time`` is now derived, but older CSV/JSON files and table
        exports still carry it. When given, it is parsed into whichever of the six
        structured fields the input did not set explicitly (explicit fields win).
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy = data.pop("deadline_time", None)
        if legacy and str(legacy).strip():
            dated = [
                k
                for k in KINDS
                if any(data.get(f"{ed}_{k}_deadline") for ed in ("prior", "upcoming"))
            ]
            for key, value in parse_legacy_deadline_time(str(legacy), dated).items():
                if data.get(key) in (None, ""):
                    data[key] = value
        return data

    @field_validator("abstract_time", "late_abstract_time", "paper_time", mode="before")
    @classmethod
    def _normalize_deadline_time(cls, value):
        return normalize_time(value) if value not in (None, "") else None

    @field_validator(
        "abstract_timezone", "late_abstract_timezone", "paper_timezone", mode="before"
    )
    @classmethod
    def _normalize_deadline_timezone(cls, value):
        return normalize_timezone(value) if value not in (None, "") else None

    @property
    def deadline_specs(self) -> dict:
        """``{kind: (time, timezone)}`` for the three deadline kinds."""
        return {
            k: (getattr(self, f"{k}_time"), getattr(self, f"{k}_timezone")) for k in KINDS
        }

    @property
    def deadline_time(self) -> Optional[str]:
        """The deadline time(s) as one display string, derived from the six fields.

        ``"11:59 PM ET"`` when every deadline the series has shares one time,
        otherwise one labeled line per deadline (``"abstract: 5:00 PM ET"`` /
        ``"paper: 11:59 PM AoE"``). ``None`` when no time is recorded. Always the
        12-hour form; the browser re-renders the structured fields in 24-hour on
        request.
        """
        dated = [
            k
            for k in KINDS
            if getattr(self, f"upcoming_{k}_deadline") or getattr(self, f"prior_{k}_deadline")
        ]
        return format_deadline_time(self.deadline_specs, dated)

    @property
    def id(self) -> str:
        """Record id for a series: the slug of its name (see :func:`name_id`)."""
        return name_id(self.name)

    @property
    def subcategory(self) -> str:
        """The subcategories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.subcategories)

    @property
    def category(self) -> str:
        """The categories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.categories)

    @property
    def format(self) -> str:
        """The formats as a single comma-joined string (for display / storage)."""
        return ", ".join(self.formats)

    @property
    def size(self) -> Optional[ConferenceSize]:
        """Size bucket, derived deterministically from :attr:`attendance`.

        A computed property, not a stored field: there is no hand-set label to
        defend, so the size is always exactly what the attendance figure implies
        (see :func:`size_for_attendance`). ``None`` when attendance is unknown.
        """
        return size_for_attendance(self.attendance)

    @property
    def attendance_display(self) -> Optional[str]:
        """Attendance formatted for display, e.g. ``"45,000 (2025)"``.

        The year the figure describes is appended in parentheses when known.
        ``None`` when no attendance figure is recorded.
        """
        if self.attendance is None:
            return None
        if self.attendance_year is not None:
            return f"{self.attendance:,} ({self.attendance_year})"
        return f"{self.attendance:,}"

    @property
    def upcoming_year(self) -> Optional[int]:
        """Year of the upcoming edition, if its start date is known."""
        return self.upcoming_start_date.year if self.upcoming_start_date else None

    @property
    def conference_month(self) -> Optional[int]:
        """Month (1-12) the conference is held, derived from its start date.

        Uses the upcoming edition's start date, falling back to the prior
        edition's -- the same date the table shows. Kept separate from the
        conference dates so rows can be sorted by season even when their years are
        offset (e.g. a meeting whose next edition is unannounced still sorts by the
        month of its most recent one).
        """
        start = self.upcoming_start_date or self.prior_start_date
        return start.month if start else None

    @property
    def conference_month_name(self) -> Optional[str]:
        """Full month name the conference is held in (e.g. ``"November"``)."""
        month = self.conference_month
        return calendar.month_name[month] if month else None

    @property
    def abstract_month(self) -> Optional[int]:
        """Month (1-12) the abstract is due, derived from the abstract deadline.

        Uses the upcoming edition's abstract deadline, falling back to the prior
        edition's -- the same date the table shows. Kept separate from the deadline
        so rows can be sorted by submission season even when their years are offset.
        """
        deadline = self.upcoming_abstract_deadline or self.prior_abstract_deadline
        return deadline.month if deadline else None

    @property
    def abstract_month_name(self) -> Optional[str]:
        """Full month name abstracts are due in (e.g. ``"April"``)."""
        month = self.abstract_month
        return calendar.month_name[month] if month else None

    @property
    def late_abstract_month(self) -> Optional[int]:
        """Month (1-12) the late abstract is due, derived from that deadline.

        Mirrors :attr:`abstract_month`: the upcoming edition's late abstract
        deadline, falling back to the prior edition's. ``None`` when the series
        has no second abstract deadline recorded.
        """
        deadline = (
            self.upcoming_late_abstract_deadline or self.prior_late_abstract_deadline
        )
        return deadline.month if deadline else None

    @property
    def late_abstract_month_name(self) -> Optional[str]:
        """Full month name late abstracts are due in (e.g. ``"October"``)."""
        month = self.late_abstract_month
        return calendar.month_name[month] if month else None

    @property
    def paper_month(self) -> Optional[int]:
        """Month (1-12) the paper is due, derived from the paper deadline.

        Mirrors :attr:`abstract_month`: the upcoming edition's paper deadline,
        falling back to the prior edition's.
        """
        deadline = self.upcoming_paper_deadline or self.prior_paper_deadline
        return deadline.month if deadline else None

    @property
    def paper_month_name(self) -> Optional[str]:
        """Full month name papers are due in (e.g. ``"May"``)."""
        month = self.paper_month
        return calendar.month_name[month] if month else None

    @property
    def location(self) -> Optional[str]:
        """Location shown in the table: upcoming, else prior."""
        return self.upcoming_location or self.prior_location

    @property
    def cost(self) -> Optional[str]:
        """Cost shown in the table: upcoming, else prior."""
        return self.upcoming_cost or self.prior_cost

    @property
    def registration(self) -> Optional[str]:
        """Registration window text shown in the table: upcoming, else prior.

        Registration is free text (windows like 'Early bird: ...; Regular: ...'),
        not a date, so there is no derived month. Mirrors the other displayed
        fields by preferring the upcoming edition's value and falling back to the
        prior edition's.
        """
        return self.upcoming_registration or self.prior_registration
