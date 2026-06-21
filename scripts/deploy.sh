#!/usr/bin/env bash
#
# Reconcile the AWS deployment with the local database, and (optionally) ship the
# latest Lambda code. Wraps scripts/push_db.py so you never have to copy the RDS
# password by hand: it resolves the function name from the CloudFormation stack
# and reads CONFERENCE_DATABASE_URL straight out of the Lambda's environment.
#
# The Vercel proxy is a static rewrite to CloudFront and never changes with the
# data or code, so it is intentionally NOT touched here (deploy it once with
# `cd deploy/vercel && vercel --prod`).
#
# Usage:
#   scripts/deploy.sh                  # push local DB -> RDS (data only)
#   DRY_RUN=1 scripts/deploy.sh        # report the row count, write nothing
#   DEPLOY_CODE=1 scripts/deploy.sh    # build + ship Lambda code, then push DB
#
# All inputs are environment variables (defaults in parentheses):
#   STACK_NAME         CloudFormation stack name              (conference-agent)
#   AWS_REGION         region the stack lives in              (us-east-1)
#   LAMBDA_LOGICAL_ID  logical id of the function in the stack (ConferenceFunction)
#   LAMBDA_FUNCTION_NAME  skip stack lookup, use this name directly  (unset)
#   SOURCE_DB          source DB URL to push from   (sqlite:///data/conferences.db)
#   DEPLOY_CODE        set to 1/true to also build + update Lambda code   (unset)
#   DRY_RUN            set to 1/true to dry-run the DB push               (unset)
#   PUSH_PYTHON        interpreter for push_db.py (needs the project env) (python)
#   SAM_BUILD_PYTHON   CPython 3.12 for `sam build` (matches the runtime) (python3.12)
set -euo pipefail

STACK_NAME="${STACK_NAME:-conference-agent}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_LOGICAL_ID="${LAMBDA_LOGICAL_ID:-ConferenceFunction}"
SOURCE_DB="${SOURCE_DB:-sqlite:///data/conferences.db}"
PUSH_PYTHON="${PUSH_PYTHON:-python}"
SAM_BUILD_PYTHON="${SAM_BUILD_PYTHON:-python3.12}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

is_true() { case "${1:-}" in 1 | true | TRUE | yes | YES) return 0 ;; *) return 1 ;; esac; }
redact() { sed -E 's#(//[^:/@]+:)[^@]+@#\1***@#g'; }

# --- Resolve the Lambda function name ---------------------------------------
FUNCTION_NAME="${LAMBDA_FUNCTION_NAME:-}"
if [[ -z "$FUNCTION_NAME" ]]; then
  echo "Resolving function name from stack '$STACK_NAME' ($AWS_REGION)..."
  FUNCTION_NAME="$(aws cloudformation describe-stack-resource \
    --stack-name "$STACK_NAME" \
    --logical-resource-id "$LAMBDA_LOGICAL_ID" \
    --region "$AWS_REGION" \
    --query 'StackResourceDetail.PhysicalResourceId' --output text)"
fi
if [[ -z "$FUNCTION_NAME" || "$FUNCTION_NAME" == "None" ]]; then
  echo "ERROR: could not resolve the Lambda function name." >&2
  echo "  Set LAMBDA_FUNCTION_NAME explicitly, or check STACK_NAME/AWS_REGION." >&2
  exit 1
fi
echo "Function: $FUNCTION_NAME"

# --- Optionally build + ship the Lambda code --------------------------------
if is_true "${DEPLOY_CODE:-}"; then
  echo
  echo "== Building Lambda package (sam build) =="
  # The Makefile build target honors an exported PYTHON; pin it to CPython 3.12
  # so compiled wheels match the python3.12 runtime.
  ( cd infra && PYTHON="$SAM_BUILD_PYTHON" sam build )

  BUILD_DIR="$REPO_ROOT/infra/.aws-sam/build/$LAMBDA_LOGICAL_ID"
  if [[ ! -d "$BUILD_DIR" ]]; then
    echo "ERROR: build output not found at $BUILD_DIR" >&2
    exit 1
  fi

  ZIP="$(mktemp -t conference-lambda-XXXXXX.zip)"
  trap 'rm -f "$ZIP"' EXIT
  echo "== Zipping $BUILD_DIR =="
  ( cd "$BUILD_DIR" && zip -qr "$ZIP" . )

  echo "== Updating function code =="
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file "fileb://$ZIP" \
    --region "$AWS_REGION" \
    --query 'LastUpdateStatus' --output text
  aws lambda wait function-updated \
    --function-name "$FUNCTION_NAME" --region "$AWS_REGION"
  echo "Code updated."
fi

# --- Fetch the RDS connection string from the Lambda env --------------------
echo
echo "== Reading CONFERENCE_DATABASE_URL from the Lambda env =="
TARGET_DB="$(aws lambda get-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --region "$AWS_REGION" \
  --query 'Environment.Variables.CONFERENCE_DATABASE_URL' --output text)"
if [[ -z "$TARGET_DB" || "$TARGET_DB" == "None" ]]; then
  echo "ERROR: the function has no CONFERENCE_DATABASE_URL env var." >&2
  exit 1
fi
echo "Target:  $(printf '%s' "$TARGET_DB" | redact)"
echo "Source:  $SOURCE_DB"

# --- Push the database ------------------------------------------------------
echo
echo "== Pushing local DB -> RDS (idempotent, fill-only upsert) =="
PUSH_ARGS=(--source "$SOURCE_DB" --target "$TARGET_DB")
if is_true "${DRY_RUN:-}"; then
  PUSH_ARGS+=(--dry-run)
fi
"$PUSH_PYTHON" scripts/push_db.py "${PUSH_ARGS[@]}"

echo
echo "Done."
