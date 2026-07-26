"""CLI bridge for the canonical MAC review finalizer.

Kubernetes review executors and host workers both finish through
``task_executor.run_deterministic_review_verdict`` so verdict semantics cannot
drift between execution substrates.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mac.task_executor import run_deterministic_review_verdict


def main() -> int:
    """Run the deterministic review finalizer entry point and return its exit code."""
    workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
    task_file = Path(os.environ.get("MAC_TASK_FILE") or workspace / "task.json")
    payload = json.loads(task_file.read_text(encoding="utf-8"))
    task = payload.get("task", payload)
    metadata = task.get("metadata") if isinstance(task, dict) else {}
    review_context = metadata.get("review_context") if isinstance(metadata, dict) else None
    if not isinstance(task, dict) or not isinstance(review_context, dict):
        raise SystemExit("review finalizer requires a review task and review_context")
    run_deterministic_review_verdict(workspace, task, review_context)
    manifest_path = workspace / "mac-evidence.json"
    if not manifest_path.exists():
        raise SystemExit("review finalizer did not produce mac-evidence.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return 0 if str(manifest.get("status") or "").lower() == "complete" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
