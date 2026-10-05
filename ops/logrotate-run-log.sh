#!/usr/bin/env bash
# Rotate data/run.log once it passes 5MB. launchd appends forever otherwise,
# and a scheduled job whose log nobody can open is a job nobody will debug.
set -euo pipefail
LOG="${1:?usage: logrotate-run-log.sh <path/to/run.log>}"
MAX=$((5 * 1024 * 1024))
[ -f "$LOG" ] || exit 0
SIZE=$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG")
if [ "$SIZE" -gt "$MAX" ]; then
  mv "$LOG" "$LOG.1"
  : > "$LOG"
fi
