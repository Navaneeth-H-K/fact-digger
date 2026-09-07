#!/usr/bin/env bash
# Quality gate used before every commit: lint, format, types, tests. Fails on the first problem.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python
[ -x "$PY" ] || PY=.venv/bin/python
"$PY" -m ruff check fkl tests scripts
"$PY" -m ruff format --check fkl tests scripts
"$PY" -m mypy
"$PY" -m pytest -q
