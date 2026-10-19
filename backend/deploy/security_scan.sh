#!/usr/bin/env bash
# Dependency and static security scans, in a throw-away environment so the project's own is left alone:
#   pip-audit: the packages requirements.txt resolves to today (what a fresh image build would install) against the known-vulnerability databases;
#   bandit:    the application and the migrations (configuration in pyproject.toml: B101 is skipped on purpose, see there).
# Needs network access. Exit status is non-zero if either finds something. Run from anywhere:  bash deploy/security_scan.sh
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
"${PYTHON:-python}" -m venv "$tmp/venv"
if [ -x "$tmp/venv/Scripts/python.exe" ]; then py="$tmp/venv/Scripts/python.exe"; else py="$tmp/venv/bin/python"; fi
"$py" -m pip install -q pip-audit bandit
echo "== pip-audit (requirements.txt)"
"$py" -m pip_audit -r requirements.txt --progress-spinner off
echo "== bandit (app, alembic)"
"$py" -m bandit -c pyproject.toml -r app alembic -q
echo "== both scans clean"
