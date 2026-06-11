#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

find_python() {
  for candidate in "$ROOT/.venv/bin/python" python3.11 python3 python; do
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      command -v "$candidate" || printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo "setup.sh: Python 3.11+ is required (.venv/bin/python, python3.11, python3, or python)" >&2
  exit 127
fi

exec "$PYTHON" "$ROOT/setup.py" "$@"
