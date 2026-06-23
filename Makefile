# Build target invoked by AWS SAM (BuildMethod: makefile) for the Lambda
# function. It copies the runtime packages into the build artifact and installs
# the Lambda dependencies there. The prod database driver is pg8000 (pure
# Python), so a plain `sam build` suffices — no container build is needed.
#
# SAM calls `make build-ConferenceFunction ARTIFACTS_DIR=<dir>` from the CodeUri
# (repo root). The target name must match the function's logical id in
# infra/template.yaml.
#
# PYTHON must be a CPython 3.12 interpreter so compiled wheels (e.g. pydantic-core)
# match the Lambda python3.12 runtime; keep it in sync with Globals.Runtime in the
# template. Override it when `python3.12` is not on PATH (e.g. a conda env that
# isn't 3.12 is active):  sam build ... or  make ... PYTHON=/path/to/python3.12
PYTHON ?= python3.12

build-ConferenceFunction:
	mkdir -p "$(ARTIFACTS_DIR)"
	cp -r conference_agent "$(ARTIFACTS_DIR)/conference_agent"
	cp -r web "$(ARTIFACTS_DIR)/web"
	$(PYTHON) -m pip install --no-cache-dir -r web/requirements.txt -t "$(ARTIFACTS_DIR)"
	# Trim files the runtime never needs to keep the package small.
	find "$(ARTIFACTS_DIR)" -type d -name "__pycache__" -prune -exec rm -rf {} + || true
	find "$(ARTIFACTS_DIR)" -type d -name "tests" -prune -exec rm -rf {} + || true

# Build target for the scheduled-refresh Lambda (EventBridge Scheduler -> Lambda,
# replacing the GitHub Actions cron). Same layout as the web function but with the
# discovery dependencies (Anthropic SDK + fetch/parse helpers) added via
# web/requirements-refresh.txt. Deployed only when EnableScheduledRefresh="true";
# the target must exist regardless so `sam build` succeeds either way.
build-RefreshFunction:
	mkdir -p "$(ARTIFACTS_DIR)"
	cp -r conference_agent "$(ARTIFACTS_DIR)/conference_agent"
	cp -r web "$(ARTIFACTS_DIR)/web"
	$(PYTHON) -m pip install --no-cache-dir -r web/requirements-refresh.txt -t "$(ARTIFACTS_DIR)"
	find "$(ARTIFACTS_DIR)" -type d -name "__pycache__" -prune -exec rm -rf {} + || true
	find "$(ARTIFACTS_DIR)" -type d -name "tests" -prune -exec rm -rf {} + || true
