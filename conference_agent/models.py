"""Typed schema for a single conference edition.

One `Conference` record describes one edition (year) of a conference. The record
id is derived from the acronym and year (e.g. ``RSNA-2026``) so that a recurring
conference produces a new row each year instead of overwriting the prior one.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ConferenceTier(str, Enum):
    """Importance / size tier of a conference.

    A controlled value (rather than free text) so calendar views and queries can
    filter and color consistently. Example: RSNA is ``big``, SPR is ``medium``.
    """

    BIG = "big"
    MEDIUM = "medium"
    SMALL = "small"


class Conference(BaseModel):
    """One edition of a conference and its key dates."""

    # Identity
    acronym: str = Field(..., description="Short name, e.g. 'RSNA'")
    name: str = Field(..., description="Full conference name")
    year: int = Field(..., description="Edition year, e.g. 2026")

    # Dates
    abstract_deadline: Optional[date] = Field(
        None, description="Abstract / submission deadline, if known"
    )
    start_date: Optional[date] = Field(None, description="First day of the conference")
    end_date: Optional[date] = Field(None, description="Last day of the conference")

    # Logistics
    location: Optional[str] = Field(None, description="City, region, country (or 'Virtual')")
    requirements: Optional[str] = Field(
        None, description="Submission / attendance requirements (free text)"
    )
    url: Optional[str] = Field(None, description="Official conference link")

    # Classification
    tier: Optional[ConferenceTier] = Field(None, description="Importance tier")
    topic: Optional[str] = Field(None, description="Field / domain, e.g. 'Radiology'")
    notes: Optional[str] = Field(None, description="Free-form notes")

    @property
    def id(self) -> str:
        """Stable record id: ``<ACRONYM>-<YEAR>`` (e.g. ``RSNA-2026``)."""
        return f"{self.acronym.upper()}-{self.year}"
