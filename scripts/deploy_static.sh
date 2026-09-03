#!/usr/bin/env bash
# Build the static bundle and publish it to Vercel production.
#
# This is the live deploy path (see CLAUDE.md "Deployment"): snapshot the DB +
# UI into dist/, then deploy dist/ to the `conferenceagent` Vercel project.
# The Vercel CLI must already be logged in (`vercel login`), or VERCEL_TOKEN
# must be set in the environment. No AWS resources are touched.
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v conda >/dev/null 2>&1 && [ "${CONDA_DEFAULT_ENV:-}" != "conference_agent" ]; then
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    conda activate conference_agent
fi

python scripts/build_static.py
cd dist
vercel deploy --prod --yes ${VERCEL_TOKEN:+--token "$VERCEL_TOKEN"}
