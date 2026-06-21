"""Typed schema for a conference.

One ``Conference`` record describes a recurring conference *series* (e.g. RSNA),
holding both its most recent **prior** edition and its **upcoming** edition. The
record id is derived from the acronym (e.g. ``RSNA``) so re-running discovery
updates the same row each cycle: as a new edition is announced, today's
"upcoming" rolls into "prior" and the freshly announced dates become "upcoming".

Keeping prior and upcoming side by side lets the table show last year's dates as
a reference even before an organizer has published next year's schedule, which is
common many months out.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ConferenceSize(str, Enum):
    """Size bucket of a conference, an objective proxy for prominence.

    A controlled value (rather than free text) so table views and queries can
    filter and color consistently. Unlike a subjective reputation judgment, the
    bucket is *derived deterministically* from a sourced attendance figure (see
    :func:`size_for_attendance`), so it is a fact that can be cited rather than an
    opinion. Example: RSNA (~45,000 attendees) is ``large``.
    """

    LARGE = "large"
    MEDIUM = "medium"
    SMALL = "small"


# Attendance thresholds (inclusive lower bounds) that bucket a conference into a
# size. Centralized so the rule is changed in one place and applied identically
# on read (the ``Conference.size`` property) and on write (the stored ``size``
# column). A meeting with >= LARGE attendees is "large"; >= MEDIUM is "medium";
# fewer is "small"; an unknown attendance yields no size.
LARGE_ATTENDANCE_THRESHOLD = 1_000
MEDIUM_ATTENDANCE_THRESHOLD = 100


def size_for_attendance(attendance: "int | None") -> "ConferenceSize | None":
    """Bucket an attendance figure into a :class:`ConferenceSize`.

    The single, deterministic size rule: ``None`` in, ``None`` out (size is left
    blank when attendance is unknown rather than guessed).
    """
    if attendance is None:
        return None
    if attendance >= LARGE_ATTENDANCE_THRESHOLD:
        return ConferenceSize.LARGE
    if attendance >= MEDIUM_ATTENDANCE_THRESHOLD:
        return ConferenceSize.MEDIUM
    return ConferenceSize.SMALL


class RemoteOption(str, Enum):
    """Whether a conference can be attended remotely."""

    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


# Subcategories are stored as a single column (a comma-joined string) but modeled
# as a list, since one conference can belong to several fields (e.g. SPR is both
# radiology and pediatrics; MICCAI is radiology and machine learning). This helper
# tokenizes any accepted form -- a list, or a delimited string -- into a clean,
# lowercased, de-duplicated list, so the model, the seed table, and the refresh
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


# The ten top-level categories. A conference's category is *derived* from its
# subcategories via :data:`SUBCATEGORY_TO_CATEGORY` -- never hand-set -- mirroring
# how ``size`` is derived from ``attendance``: there is a single mapping to
# maintain, and the broad bucket can never drift from the granular tags it is
# computed from.
CATEGORIES = (
    "humanities",
    "social science",
    "medicine",
    "biology",
    "chemistry",
    "physics",
    "math",
    "stats",
    "computer science",
    "artificial intelligence",
)

# Granular subcategory -> top-level category. Every subcategory used in
# ``config.SEED_CONFERENCES`` must appear here (a test enforces this), and every
# value must be one of :data:`CATEGORIES`. Adding a new subcategory is a single
# entry here; the category column then derives automatically. Most subcategories
# are clinical specialties under "medicine"; the cross-domain fields (genomics,
# machine learning) bucket into biology and artificial intelligence.
SUBCATEGORY_TO_CATEGORY = {
    # --- medicine: clinical specialties and medicine-adjacent fields ---------
    "radiology": "medicine",
    "anesthesiology": "medicine",
    "cardiology": "medicine",
    "dermatology": "medicine",
    "emergency medicine": "medicine",
    "endocrinology": "medicine",
    "family medicine": "medicine",
    "gastroenterology": "medicine",
    "internal medicine": "medicine",
    "neurology": "medicine",
    "obstetrics and gynecology": "medicine",
    "oncology": "medicine",
    "ophthalmology": "medicine",
    "orthopedics": "medicine",
    "pediatrics": "medicine",
    "psychiatry": "medicine",
    "pulmonology": "medicine",
    "surgery": "medicine",
    "urology": "medicine",
    "allergy and immunology": "medicine",
    "critical care medicine": "medicine",
    "geriatrics": "medicine",
    "hematology": "medicine",
    "infectious disease": "medicine",
    "medical physics": "medicine",
    "nephrology": "medicine",
    "neurosurgery": "medicine",
    "otolaryngology": "medicine",
    "palliative care": "medicine",
    "pathology": "medicine",
    "physical medicine and rehabilitation": "medicine",
    "plastic surgery": "medicine",
    "public health": "medicine",
    "radiation oncology": "medicine",
    "rheumatology": "medicine",
    "sports medicine": "medicine",
    # --- biology -------------------------------------------------------------
    "genomics": "biology",
    "biophysics": "biology",
    "biochemistry": "biology",
    "cell biology": "biology",
    # --- chemistry -----------------------------------------------------------
    "chemistry": "chemistry",
    "analytical chemistry": "chemistry",
    "drug discovery": "chemistry",
    # --- physics -------------------------------------------------------------
    "physics": "physics",
    "astrophysics": "physics",
    "optics": "physics",
    # --- math ----------------------------------------------------------------
    "mathematics": "math",
    "applied mathematics": "math",
    # --- stats ---------------------------------------------------------------
    "statistics": "stats",
    "biostatistics": "stats",
    "data science": "stats",
    # --- computer science ----------------------------------------------------
    "software engineering": "computer science",
    "programming languages": "computer science",
    "theoretical computer science": "computer science",
    "computer graphics": "computer science",
    "simulation": "computer science",
    # --- artificial intelligence ---------------------------------------------
    "machine learning": "artificial intelligence",
}


def categories_for_subcategories(subcategories: "list | tuple | None") -> List[str]:
    """Top-level categories implied by a list of subcategories.

    Each subcategory maps to exactly one category via
    :data:`SUBCATEGORY_TO_CATEGORY`; the result is the de-duplicated set of those
    categories, returned in canonical :data:`CATEGORIES` order so the column reads
    consistently. A subcategory with no mapping is skipped (it contributes no
    category) rather than raising, so an unfamiliar tag degrades gracefully.
    """
    present = set()
    for sub in subcategories or []:
        category = SUBCATEGORY_TO_CATEGORY.get(sub)
        if category:
            present.add(category)
    return [c for c in CATEGORIES if c in present]


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


class Conference(BaseModel):
    """A recurring conference series with its prior and upcoming editions."""

    model_config = ConfigDict(populate_by_name=True)

    # --- Identity ----------------------------------------------------------
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name")
    # One conference can carry several subcategory tags (e.g. SPR -> radiology +
    # pediatrics). Accepts either a list or a comma/semicolon-delimited string (and
    # the singular ``subcategory`` key) on input; ``subcategory`` below exposes the
    # joined string for display and storage. The broad ``category`` (one of
    # :data:`CATEGORIES`) is derived from these, not taken as input.
    subcategories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subcategories", "subcategory"),
        description="Specific field(s), e.g. ['radiology', 'machine learning']",
    )

    @field_validator("subcategories", mode="before")
    @classmethod
    def _normalize_subcategories(cls, value):
        return normalize_subcategories(value)

    # --- Prior (most recent completed) edition -----------------------------
    prior_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the most recent edition"
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

    # --- Upcoming edition --------------------------------------------------
    upcoming_abstract_deadline: Optional[date] = Field(
        None, description="Abstract submission deadline of the upcoming edition"
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

    # --- Logistics & classification ----------------------------------------
    location: Optional[str] = Field(
        None, description="Host city / venue, e.g. 'Chicago, IL' or 'Vienna, Austria'"
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

    @field_validator("formats", mode="before")
    @classmethod
    def _normalize_formats(cls, value):
        return normalize_formats(value)
    cost: Optional[str] = Field(
        None, description="Registration cost summary, e.g. '$1,095 (member, early-bird)'"
    )
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

    @property
    def id(self) -> str:
        """Stable record id for a series: the upper-cased acronym (e.g. ``RSNA``)."""
        return self.acronym.upper()

    @property
    def subcategory(self) -> str:
        """The subcategories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.subcategories)

    @property
    def categories(self) -> List[str]:
        """Top-level categories, derived from :attr:`subcategories`.

        A computed property, never stored as input: each subcategory maps to one
        of :data:`CATEGORIES` via :data:`SUBCATEGORY_TO_CATEGORY`, and the result
        is the de-duplicated set in canonical order (see
        :func:`categories_for_subcategories`). Multi-domain series carry several --
        e.g. MICCAI (radiology + machine learning) -> ['medicine',
        'artificial intelligence'].
        """
        return categories_for_subcategories(self.subcategories)

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
    def registration(self) -> Optional[str]:
        """Registration window text shown in the table: upcoming, else prior.

        Registration is free text (windows like 'Early bird: ...; Regular: ...'),
        not a date, so there is no derived month. Mirrors the other displayed
        fields by preferring the upcoming edition's value and falling back to the
        prior edition's.
        """
        return self.upcoming_registration or self.prior_registration
