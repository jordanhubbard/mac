#!/usr/bin/env python3
"""Generate/check the current PostgreSQL bootstrap from ordered migrations."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.schema_migrations import SCHEMA_PATH, render_bootstrap_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render_bootstrap_schema()
    if args.write:
        SCHEMA_PATH.write_text(rendered, encoding="utf-8")
        return 0
    current = SCHEMA_PATH.read_text(encoding="utf-8")
    if current != rendered:
        print(
            "schema.sql drifted from immutable ordered migrations; "
            "run make postgres-schema ARGS=--write",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
