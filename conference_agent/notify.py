"""Email notifications.

Sends a short "table refreshed" email after a discovery / daily run. SMTP
settings and the destination address come from the environment (see
``conference_agent.config``) so no credentials are committed. If SMTP is not
configured, :func:`send_email` is a no-op that returns ``False`` rather than
raising, so a missing mailbox never breaks a refresh.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Iterable, Optional

from conference_agent.config import (
    NOTIFY_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)
from conference_agent.models import Conference


def send_email(subject: str, body: str, to_address: Optional[str] = None) -> bool:
    """Send a plain-text email. Returns ``True`` if sent, ``False`` if skipped.

    Skips silently (returns ``False``) when ``SMTP_USER`` / ``SMTP_PASSWORD`` are
    not set, so callers can always invoke it without guarding.
    """
    to_address = to_address or NOTIFY_EMAIL
    if not (SMTP_USER and SMTP_PASSWORD and to_address):
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SMTP_USER
    message["To"] = to_address
    message.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)
    return True


def _summarize(conferences: Iterable[Conference]) -> str:
    """Build a readable one-line-per-conference summary."""
    lines = []
    for conf in conferences:
        when = conf.upcoming_start_date.isoformat() if conf.upcoming_start_date else "TBA"
        deadline = (
            conf.upcoming_abstract_deadline.isoformat()
            if conf.upcoming_abstract_deadline
            else "TBA"
        )
        lines.append(
            f"- {conf.acronym} ({conf.subcategory}): conference {when}, abstract deadline {deadline}"
        )
    return "\n".join(lines)


def notify_refresh(conferences: Iterable[Conference], written: int) -> bool:
    """Email a summary of a completed table refresh. Returns send status."""
    conferences = list(conferences)
    subject = f"Conference table refreshed — {written} record(s)"
    body = (
        f"The conference table was refreshed with {written} record(s).\n\n"
        f"{_summarize(conferences)}\n"
    )
    return send_email(subject, body)
