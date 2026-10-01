"""Add merged "Add or edit a conference" submissions to the database.

The site's form (``web/static/add.html`` -> ``api/propose``) opens a pull request
that adds ``submissions/<id>-<date>-<rand>.json``, an ``add --json`` record, or,
for an edit of a listed conference, ``submissions/<id>-<date>-<rand>.update.json``
(the name plus only the changed fields).
Once a maintainer merges it, the daily job (``scripts/scheduled_discovery.sh``)
runs this script, which:

1. fetches the base branch (default ``origin/main``);
2. lists ``submissions/*.json`` on it and reads each file with ``git show``, so
   the working tree and the checked-out branch are never touched; and
3. runs ``conference-agent add --json`` on every file not processed before
   (``add --update --json`` for an ``.update.json`` edit, which keeps every
   field the file does not name).

Each processed file is recorded in ``data/ingested_submissions.json`` with its
outcome. A file that fails (for example, the name already exists because it was
added by hand) is recorded and not retried; delete its entry to retry it. The
exit status is 1 when a file failed in this run (or the fetch failed), so the
job's failure alert reports it once.

Usage::

    python scripts/ingest_submissions.py [--db URL] [--remote origin] [--branch main] [--dry-run]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from conference_agent.cli import main as cli_main
from conference_agent.config import DEFAULT_DATABASE_URL

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_DIR), *args], check=True, capture_output=True, text=True
    ).stdout


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _add(text: str, db_url: str, update: bool = False) -> tuple[int, str]:
    """Run ``add [--update] --json`` on one submission; return (exit code, output)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        fh.write(text)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli_main(["--db", db_url, "add", *(["--update"] if update else []), "--json", fh.name])
    except SystemExit as exc:  # argparse / validation exits
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # a malformed file must not stop the others
        code, out = 1, io.StringIO(f"{type(exc).__name__}: {exc}")
    finally:
        os.unlink(fh.name)
    return code, out.getvalue().strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Add merged form submissions to the database.")
    parser.add_argument(
        "--db",
        default=os.environ.get("CONFERENCE_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="SQLAlchemy URL",
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--state", default=PROJECT_DIR / "data" / "ingested_submissions.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="List new submissions without adding them.")
    args = parser.parse_args()

    ref = f"{args.remote}/{args.branch}"
    try:
        _git("fetch", "--quiet", args.remote, args.branch)
        listing = _git("ls-tree", "--name-only", ref, "submissions/")
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: could not read submissions from {ref}: {exc.stderr.strip()}")
        return 1

    state = _load(args.state)
    new = [p for p in listing.split() if p.endswith(".json") and p not in state]
    if not new:
        print(f"No new submissions on {ref}.")
        return 0

    failed = 0
    for path in sorted(new):
        if args.dry_run:
            print(f"Would add {path}")
            continue
        code, output = _add(_git("show", f"{ref}:{path}"), args.db, update=path.endswith(".update.json"))
        ok = code == 0
        failed += not ok
        state[path] = {"date": date.today().isoformat(), "ok": ok, "output": output}
        print(f"{'Added' if ok else 'FAILED'} {path}" + (f": {output}" if output else ""))
    if not args.dry_run:
        _save(args.state, state)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
