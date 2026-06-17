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


class ConferenceTier(str, Enum):
    """Quality / reputability tier of a conference.

    A controlled value (rather than free text) so table views and queries can
    filter and color consistently. Example: RSNA is ``big``, SPR is ``medium``.
    """

    BIG = "big"
    MEDIUM = "medium"
    SMALL = "small"


class RemoteOption(str, Enum):
    """Whether a conference can be attended remotely."""

    IN_PERSON = "in-person"
    VIRTUAL = "virtual"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


# Categories are stored as a single column (a comma-joined string) but modeled as
# a list, since one conference can belong to several fields (e.g. SPR is both
# radiology and pediatrics; MICCAI is radiology and machine learning). This helper
# normalizes any accepted form -- a list, or a delimited string -- into a clean,
# lowercased, de-duplicated list, so the model, the seed table, and the refresh
# policy all split categories the same way.
def normalize_categories(value: "str | list | tuple | None") -> List[str]:
    """Normalize categories to a lowercased, de-duplicated, order-preserving list."""
    if value is None:
        parts: List[str] = []
    elif isinstance(value, str):
        parts = re.split(r"[;,]", value)
    else:
        parts = [p for item in value for p in re.split(r"[;,]", str(item))]
    seen: set[str] = set()
    out: List[str] = []
    for part in parts:
        cat = part.strip().lower()
        if cat and cat not in seen:
            seen.add(cat)
            out.append(cat)
    return out


class Conference(BaseModel):
    """A recurring conference series with its prior and upcoming editions."""

    model_config = ConfigDict(populate_by_name=True)

    # --- Identity ----------------------------------------------------------
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name")
    # One conference can carry several tags (e.g. SPR -> radiology + pediatrics).
    # Accepts either a list or a comma/semicolon-delimited string (and the legacy
    # singular ``category`` key) on input; ``category`` below exposes the joined
    # string for display and storage.
    categories: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("categories", "category"),
        description="Domain(s) / field(s), e.g. ['radiology', 'machine learning']",
    )

    @field_validator("categories", mode="before")
    @classmethod
    def _normalize_categories(cls, value):
        return normalize_categories(value)

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

    # --- Logistics & classification ----------------------------------------
    location: Optional[str] = Field(
        None, description="Host city / venue, e.g. 'Chicago, IL' or 'Vienna, Austria'"
    )
    url: Optional[str] = Field(None, description="Official conference website link")
    remote_option: Optional[RemoteOption] = Field(
        None, description="In-person / virtual / hybrid attendance option"
    )
    cost: Optional[str] = Field(
        None, description="Registration cost summary, e.g. '$1,095 (member, early-bird)'"
    )
    reputation: Optional[ConferenceTier] = Field(
        None, description="Quality / reputability tier (big / medium / small)"
    )
    notes: Optional[str] = Field(None, description="Free-form notes")

    @property
    def id(self) -> str:
        """Stable record id for a series: the upper-cased acronym (e.g. ``RSNA``)."""
        return self.acronym.upper()

    @property
    def category(self) -> str:
        """The categories as a single comma-joined string (for display / storage)."""
        return ", ".join(self.categories)

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
