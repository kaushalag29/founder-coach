#!/bin/sh
# ytbrain ops all: see docs/ops.md. Resumes where the last run stopped; extra flags pass through
# (e.g. --dry-run, --max-cost 3, --skip-coach, --restart).
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/ytbrain ]; then exec .venv/bin/ytbrain ops all "$@"; fi
exec ytbrain ops all "$@"
