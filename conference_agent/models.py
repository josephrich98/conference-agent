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

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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


class Conference(BaseModel):
    """A recurring conference series with its prior and upcoming editions."""

    # --- Identity ----------------------------------------------------------
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name")
    category: str = Field(
        ..., description="Domain / field, e.g. 'radiology', 'genomics', 'AI'"
    )

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
    def upcoming_year(self) -> Optional[int]:
        """Year of the upcoming edition, if its start date is known."""
        return self.upcoming_start_date.year if self.upcoming_start_date else None
