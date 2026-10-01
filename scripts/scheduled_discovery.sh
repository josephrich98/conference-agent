#!/bin/bash
# Scheduled discovery: runs daily (cron, 2 AM).
#
# Runs the page-gated watch cadence (daily_update.py --cadence watch): series
# with a submission deadline within two weeks either side are page-checked every
# day; series with a deadline or meeting in the next month, or waiting on a next
# edition (6-24 months after the last one), every two weeks. The agent (claude-code
# backend: local Claude Code subscription, no API key) only re-researches series
# whose official pages changed -- see conference_agent/refresh.py.
#
# Before the watch run it adds any "Add or edit a conference" submissions merged into
# origin/main (scripts/ingest_submissions.py). The watch run also moves finished
# editions into the prior columns.
#
# If the run changes what the site shows, it redeploys the static site
# (scripts/deploy_static.sh). The comparison is on the exported site data, not
# the DB file, because every run updates per-row bookkeeping columns.
#
# After the redeploy it emails subscribers (updates and one-week deadline
# reminders). If any step fails, the end of the log is emailed to
# CONFERENCE_NOTIFY_EMAIL (scripts/alert_failure.py).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/data/logs"
mkdir -p "$LOG_DIR"

# cron starts with a minimal PATH (/usr/bin:/bin), which lacks the `claude` CLI
# (~/.local/bin) and the nvm-managed `vercel` CLI. Add both explicitly.
export PATH="$HOME/.local/bin:$PATH"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null
# Write progress to the log as it happens rather than in one block at exit.
export PYTHONUNBUFFERED=1
# Email credentials (SMTP_USER, SMTP_PASSWORD, SUBSCRIBE_SECRET) live outside
# the repo, since cron's environment has none of them. Without them the refresh
# still runs; only the emails are skipped.
SECRETS_FILE="${CONFERENCE_SECRETS_FILE:-$HOME/.config/conference-agent/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SECRETS_FILE"
  set +a
fi

# One run at a time: a run that is still researching when the next one fires
# makes the next one exit quietly.
exec 9>"$LOG_DIR/.scheduled_discovery.lock"
flock -n 9 || exit 0

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$LOG_DIR/discovery_${TIMESTAMP}.log"
SNAP_DIR="$(mktemp -d)"
trap 'rm -rf "$SNAP_DIR"' EXIT

run() {
  echo "=== Scheduled Discovery Start: $TIMESTAMP ==="
  echo "Project: $PROJECT_DIR"
  echo ""

  for tool in claude vercel; do
    command -v "$tool" >/dev/null || { echo "ERROR: '$tool' not found on PATH ($PATH)"; return 1; }
  done

  eval "$(conda shell.bash hook)"
  conda activate conference_agent || return 1
  # The nvm-managed node links against libatomic, which this system only has
  # inside conda's lib directory (an interactive shell gets this from .bashrc).
  export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:$HOME/.local/lib:${LD_LIBRARY_PATH:-}"
  cd "$PROJECT_DIR" || return 1

  # Hash of the data the site shows, before and after the run.
  site_hash() {
    python scripts/build_static.py --out "$SNAP_DIR" >/dev/null &&
      sha256sum "$SNAP_DIR/data/conferences.json" | awk '{print $1}'
  }
  local before after status
  before="$(site_hash)" || return 1

  # Merged form submissions. A failure is reported but does not stop the run.
  status=0
  python scripts/ingest_submissions.py || { echo "ERROR: submission ingest failed"; status=1; }
  echo ""

  python scripts/daily_update.py --cadence watch --backend claude-code || status=1

  # Deploy even after a partial failure: batches that succeeded are kept.
  after="$(site_hash)" || return 1
  echo ""
  local deployed=1
  if [ "$before" != "$after" ]; then
    echo "Site data changed -- redeploying via scripts/deploy_static.sh"
    bash scripts/deploy_static.sh || { echo "ERROR: deploy failed"; status=1; deployed=0; }
  else
    echo "Site data unchanged -- no redeploy"
  fi

  # Email visitors subscribed to a changed conference, once the site shows the
  # change. Runs even when nothing changed this time, to retry failed sends.
  if [ "$deployed" -eq 1 ]; then
    echo ""
    python scripts/notify_subscribers.py || { echo "ERROR: subscriber emails failed"; status=1; }
  fi
  return "$status"
}

run >> "$LOG_FILE" 2>&1
STATUS=$?
if [ "$STATUS" -eq 0 ]; then
  echo "=== Scheduled Discovery Complete: $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
else
  echo "=== Scheduled Discovery FAILED (exit $STATUS): $(date +%Y-%m-%d_%H-%M-%S) ===" >> "$LOG_FILE"
  # Email the end of the log. run() may have failed before activating the env.
  {
    if [ "${CONDA_DEFAULT_ENV:-}" != "conference_agent" ]; then
      eval "$(conda shell.bash hook 2>/dev/null)" && conda activate conference_agent
    fi
    cd "$PROJECT_DIR" && python scripts/alert_failure.py "$LOG_FILE" "$STATUS"
  } >> "$LOG_FILE" 2>&1
fi
exit "$STATUS"
