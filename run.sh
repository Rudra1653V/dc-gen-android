#!/usr/bin/env bash
# dc-gen-android launcher. Always uses the venv, never the system python.
#
#   ./run.sh main.py --dry-run
#   ./run.sh -m tools.doctor
#   ./run.sh -m tools.walk 0 --email you+dcgen1@gmail.com
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "no interpreter at $PY" >&2
  echo "create it:  python3 -m venv .venv && .venv/bin/pip install pyyaml" >&2
  exit 1
fi

export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
exec "$PY" "$@"