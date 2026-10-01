"""Email the maintainer that the scheduled job failed, with the end of its log.

Called by ``scripts/scheduled_discovery.sh`` when a run exits non-zero. Sends to
``CONFERENCE_NOTIFY_EMAIL`` (see ``conference_agent.config``) over the SMTP
settings the other emails use; without SMTP credentials it prints a note and
exits 0, since there is nothing else to do.

Usage::

    python scripts/alert_failure.py LOG_FILE EXIT_STATUS [--lines 80]
"""

from __future__ import annotations

import argparse
import socket
import sys
from collections import deque
from pathlib import Path

from conference_agent.notify import send_email


def main() -> int:
    parser = argparse.ArgumentParser(description="Email a scheduled-job failure alert.")
    parser.add_argument("log_file", type=Path)
    parser.add_argument("status")
    parser.add_argument("--lines", type=int, default=80, help="Log lines to include (default 80).")
    args = parser.parse_args()

    try:
        with args.log_file.open(encoding="utf-8", errors="replace") as fh:
            tail = "".join(deque(fh, maxlen=args.lines))
    except OSError as exc:
        tail = f"(could not read the log: {exc})\n"

    body = (
        f"The scheduled Conference Agent job on {socket.gethostname()} exited with "
        f"status {args.status}.\n\n"
        f"Full log: {args.log_file}\n\n"
        f"Last {args.lines} lines:\n\n{tail}"
    )
    try:
        sent = send_email(f"Conference Agent daily job failed (exit {args.status})", body)
    except Exception as exc:
        print(f"Failure alert could not be sent: {exc}")
        return 1
    print("Failure alert sent." if sent else "Failure alert skipped (SMTP not configured).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
