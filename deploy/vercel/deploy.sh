#!/usr/bin/env bash
# Guarded deploy for the RETIRED AWS-fronting proxy.
#
# This directory holds a Vercel rewrite that forwards every request to the AWS
# CloudFront distribution. The public site is the static bundle in `dist/`,
# linked to the `conferenceagent` Vercel project. If this proxy is ever deployed
# to `conferenceagent`, it would overwrite the live static site with a proxy to
# AWS. This script refuses to do that: it only deploys when the linked project
# is something OTHER than `conferenceagent`.
set -euo pipefail

cd "$(dirname "$0")"

PROTECTED_PROJECT="conferenceagent"
LINK_FILE=".vercel/project.json"

if [[ ! -f "$LINK_FILE" ]]; then
  echo "ERROR: this directory is not linked to a Vercel project." >&2
  echo "Link it to a SEPARATE project first, never '$PROTECTED_PROJECT':" >&2
  echo "  npx vercel link --yes --project conferenceagent-aws" >&2
  exit 1
fi

# Extract projectName without assuming jq is installed.
PROJECT_NAME="$(sed -n 's/.*"projectName":"\([^"]*\)".*/\1/p' "$LINK_FILE")"

if [[ "$PROJECT_NAME" == "$PROTECTED_PROJECT" ]]; then
  echo "REFUSING TO DEPLOY: this directory is linked to '$PROTECTED_PROJECT'," >&2
  echo "which is the public static site (served from dist/). Deploying this" >&2
  echo "CloudFront proxy there would take the live site offline." >&2
  echo "Unlink and relink to a separate project (e.g. conferenceagent-aws)" >&2
  echo "before deploying:" >&2
  echo "  rm -rf .vercel && npx vercel link --yes --project conferenceagent-aws" >&2
  exit 1
fi

echo "Deploying CloudFront proxy to Vercel project '$PROJECT_NAME'..."
exec npx vercel --prod --yes "$@"
