#!/bin/sh
# What CI runs (.github/workflows/ci.yml), on your machine, before you push:
#   sh scripts/check.sh                 # secret scan, the seven offline suites, generated files, plugin validator
#   PYTHON=/path/to/python sh scripts/check.sh
# As a git pre-commit hook it runs on every `git commit`; `git commit --no-verify` skips it (docs/release.md).
#   SKIP_VALIDATE=1 sh scripts/check.sh # without `claude plugin validate`
# Offline and free: no .env, no model calls, no writes to data/ or dist/. Stops at the first failure.
set -eu
cd "$(dirname "$0")/.."
# Python: $PYTHON, else the active venv, else the repo's .venv (git may run this hook from an IDE with no venv), else python3.
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then PY="$VIRTUAL_ENV/bin/python"
elif [ -x .venv/bin/python ] && .venv/bin/python -c "" 2>/dev/null; then PY=".venv/bin/python"
else PY="python3"; fi
echo "python: $PY"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export REQUIRE_ALL_TESTS=1 YTBRAIN_DOTENV=0 PYTHONDONTWRITEBYTECODE=1 YTBRAIN_ROOT="$TMP/data"

echo "== secret scan"
"$PY" scripts/check_secrets.py

for s in core eval pack coach plugin web books; do
  echo "== test_$s"
  "$PY" "tests/test_$s.py" > "$TMP/$s.log" 2>&1 || { grep -v "^  PASS" "$TMP/$s.log" | tail -40; echo "FAILED: test_$s (a 'skipped in CI' line means an extra is missing: uv pip install -e \".[serve,pack,dev,web]\" \"lancedb>=0.39.0\")"; exit 1; }
  tail -1 "$TMP/$s.log"
  if grep -q "(skipped" "$TMP/$s.log"; then echo "FAILED: test_$s skipped tests (install the extras: see AGENTS.md)"; exit 1; fi
done

echo "== all suites in one process (what \`pytest tests\` does: catches state leaking between suites)"
"$PY" -m pytest tests -q -p no:cacheprovider > "$TMP/pytest.log" 2>&1 || { tail -40 "$TMP/pytest.log"; echo "FAILED: the suites pass alone but not together"; exit 1; }
tail -1 "$TMP/pytest.log"

echo "== plugin assembles from a test pack; generated files are fresh"
before="$(cat founder_coach/product.json founder_coach/playbooks/*.md | cksum)"
(cd "$TMP" && PYTHONPATH="$OLDPWD/tests" "$PY" -c "
from pathlib import Path
from test_pack import HashEmbed, _build
_build(Path('$TMP/pack'), emb=HashEmbed())")
"$PY" scripts/assemble_plugin.py --pack "$TMP/pack" --out "$TMP/plugin" --check > "$TMP/assemble.log" 2>&1 || { cat "$TMP/assemble.log"; exit 1; }
after="$(cat founder_coach/product.json founder_coach/playbooks/*.md | cksum)"
if [ "$before" != "$after" ]; then
  echo "FAILED: founder_coach/product.json or playbooks/ were stale; the assembler just regenerated them. Review and commit."; exit 1
fi

if [ "${SKIP_VALIDATE:-0}" != "1" ] && command -v claude >/dev/null 2>&1; then
  echo "== claude plugin validate"
  claude plugin validate "$TMP/plugin" --strict
else
  echo "== claude plugin validate: skipped (no claude on PATH, or SKIP_VALIDATE=1)"
fi
echo "ALL CHECKS PASSED: safe to commit and push"
