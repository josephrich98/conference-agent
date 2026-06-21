#!/bin/bash
# Scheduled discovery: runs every 2 weeks (Saturday 2 AM)
# Discovers conferences due for auto-check (6-12 month staleness window)
# Uses claude-code backend (local Claude Code subscription, no API key)
# If the discovery run changes the local DB, it pushes to AWS (scripts/deploy.sh)
#
# Cron fires this weekly; the parity guard below skips odd ISO weeks so the
# job effectively runs every other Saturday. (Cron can't express "every 2
# weeks" directly since weeks don't divide evenly into months.)

set -e

# Biweekly guard: only proceed on even ISO week numbers. Strip leading zero so
# values like "08" aren't parsed as invalid octal.
WEEK=$((10#$(date +%V)))
if (( WEEK % 2 != 0 )); then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/data/logs"

mkdir -p "$LOG_DIR"
TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
LOG_FILE="$LOG_DIR/discovery_${TIMESTAMP}.log"

{
  echo "=== Scheduled Discovery Start: $TIMESTAMP ==="
  echo "Project: $PROJECT_DIR"
  echo ""

  # Activate conda environment
  eval "$(conda shell.bash hook)"
  conda activate conference_agent

  # Run discovery: --cadence due targets only conferences due for auto-check
  cd "$PROJECT_DIR"

  # Hash the local DB before and after so we only push to AWS when it changed.
  DB_FILE="$PROJECT_DIR/data/conferences.db"
  db_hash() { [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo "missing"; }
  DB_HASH_BEFORE="$(db_hash "$DB_FILE")"

  python scripts/daily_update.py --cadence due --backend claude-code

  DB_HASH_AFTER="$(db_hash "$DB_FILE")"
  echo ""
  if [ "$DB_HASH_BEFORE" != "$DB_HASH_AFTER" ]; then
    echo "Database changed — pushing to AWS via scripts/deploy.sh"
    bash scripts/deploy.sh || echo "WARNING: scripts/deploy.sh failed (exit $?)"
  else
    echo "Database unchanged — skipping AWS push"
  fi

  echo ""
  echo "=== Scheduled Discovery Complete: $(date +%Y-%m-%d_%H-%M-%S) ==="
} >> "$LOG_FILE" 2>&1

exit 0
