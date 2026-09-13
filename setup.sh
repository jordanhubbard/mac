#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

PYTHON_VERSION="$(cat "$ROOT/.python-version")"

find_python() {
  for candidate in "$ROOT/.venv/bin/python" python3.14 python3 python; do
    if "$candidate" -c 'import platform,sys; raise SystemExit(platform.python_version() != sys.argv[1])' "$PYTHON_VERSION" >/dev/null 2>&1; then
      command -v "$candidate" || printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON="$(find_python)"; then
  echo "setup.sh: Python $PYTHON_VERSION is required; run uv python install" >&2
  exit 127
fi

exec "$PYTHON" "$ROOT/setup.py" "$@"
