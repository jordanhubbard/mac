"""Best-effort worker integration for external activation-probe results."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .classifier import ActivationProbeClassifier

logger = logging.getLogger("mac.activation_probe")


def _enabled(env: Mapping[str, str]) -> bool:
    return str(env.get("MAC_ACTIVATION_PROBE_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _activation_input(
    task_dir: Path, execution_metadata: Mapping[str, Any], env: Mapping[str, str]
) -> Any:
    if "activation_probe_activations" in execution_metadata:
        return execution_metadata["activation_probe_activations"]
    configured = str(env.get("MAC_ACTIVATION_PROBE_ACTIVATIONS_FILE") or "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else task_dir / "activation-probe-activations.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        return data.get("activations")
    return data


def activation_probe_audit_from_environment(
    task_dir: Path,
    execution_metadata: Mapping[str, Any],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[dict[str, Any]]:
    """Return an advisory result or ``None``; never raise into task execution."""
    environment = os.environ if env is None else env
    if not _enabled(environment):
        return None
    started = time.perf_counter()
    try:
        checkpoint = str(environment.get("MAC_ACTIVATION_PROBE_CHECKPOINT") or "").strip() or None
        classifier = ActivationProbeClassifier.load(checkpoint)
        prediction = (
            classifier.predict(_activation_input(task_dir, execution_metadata, environment))
            if classifier.enabled
            else classifier.predict([])
        )
        return {
            **prediction.to_dict(),
            "runtime_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "advisory_only": True,
            "schema": "mac.activation_probe.audit.v1",
        }
    except Exception as exc:  # noqa: BLE001 - advisory analysis must never fail work.
        logger.warning("external activation probe skipped after error: %s", exc)
        return None
