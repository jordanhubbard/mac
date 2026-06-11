#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mac.project_inception import run_c26_project_inception_proof
from mac.services import ControlPlane
from mac.store import SQLiteStore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="run the MAC c26 project inception lifecycle proof"
    )
    parser.add_argument("--project-path", default="~/Src/c26")
    parser.add_argument("--db", help="optional SQLite database path for durable proof state")
    parser.add_argument("--output", help="optional JSON file to write the proof artifact")
    args = parser.parse_args()
    cp = (
        ControlPlane(
            SQLiteStore(args.db),
            secret_key=os.environ.get("MAC_SECRET_KEY")
            or "local-c26-inception-proof-secret-key-32chars",
        )
        if args.db
        else None
    )
    proof = run_c26_project_inception_proof(cp, project_path=args.project_path)
    rendered = json.dumps(proof, indent=2, sort_keys=True) + "\n"
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if proof.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
