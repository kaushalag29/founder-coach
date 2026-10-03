#!/bin/sh
# ytbrain ops eval: see docs/ops.md. Resumes where the last run stopped; extra flags pass through
# (e.g. --dry-run, --max-cost 3, --skip-coach, --restart).
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/bin/ytbrain ]; then exec .venv/bin/ytbrain ops eval "$@"; fi
exec ytbrain ops eval "$@"
