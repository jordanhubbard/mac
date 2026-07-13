"""Staged edge-node fleet rollout plan for OpenClaw.

Provides a data class for a rollout plan step, a function to build a staged
deploy plan from a list of edge-node targets, and a function to execute
(or simulate) a staged rollout with canary/promote logic.

Stages:
  canary   — first node, gated before wider promotion
  promote  — remaining nodes rolled out after canary health passes

Status values: planned, active, succeeded, failed, skipped
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class RolloutPlanStep:
    """A single node's position in a staged OpenClaw fleet rollout."""

    node_id: str
    host: str
    stage: str  # "canary" | "promote"
    status: str  # "planned" | "active" | "succeeded" | "failed" | "skipped"


@dataclass
class RolloutPlan:
    """Complete staged rollout plan for an OpenClaw fleet."""

    version: str
    steps: List[RolloutPlanStep] = field(default_factory=list)

    # Derived helpers -------------------------------------------------------

    @property
    def canary_steps(self) -> List[RolloutPlanStep]:
        return [s for s in self.steps if s.stage == "canary"]

    @property
    def promote_steps(self) -> List[RolloutPlanStep]:
        return [s for s in self.steps if s.stage == "promote"]

    def step_for_node(self, node_id: str) -> Optional[RolloutPlanStep]:
        for step in self.steps:
            if step.node_id == node_id:
                return step
        return None


@dataclass
class RolloutResult:
    """Summary of a completed (or stopped) rollout execution."""

    version: str
    succeeded: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def build_staged_rollout_plan(
    version: str,
    targets: List[Dict[str, str]],
    *,
    canary_count: int = 1,
) -> RolloutPlan:
    """Build a staged deploy plan from a list of edge-node targets.

    Each entry in *targets* must have at minimum ``node_id`` and ``host``
    keys.  The first *canary_count* nodes become the "canary" stage; the
    remainder become the "promote" stage.

    Args:
        version: Artifact version being rolled out.
        targets: Ordered list of ``{"node_id": ..., "host": ...}`` dicts.
        canary_count: Number of leading nodes to treat as canary.  Must be
            >= 1 and <= len(targets).  Defaults to 1.

    Returns:
        A :class:`RolloutPlan` with all steps set to ``"planned"``.

    Raises:
        ValueError: If *targets* is empty, *canary_count* is out of range,
            or a target entry is missing required keys.
    """
    if not version or not version.strip():
        raise ValueError("rollout version is required")
    if not targets:
        raise ValueError("at least one target node is required")
    canary_count = int(canary_count)
    if canary_count < 1:
        raise ValueError("canary_count must be at least 1")
    if canary_count > len(targets):
        raise ValueError(
            "canary_count (%d) exceeds number of targets (%d)" % (canary_count, len(targets))
        )

    steps: List[RolloutPlanStep] = []
    for index, target in enumerate(targets):
        node_id = str(target.get("node_id") or "").strip()
        host = str(target.get("host") or "").strip()
        if not node_id:
            raise ValueError("target at index %d is missing 'node_id'" % index)
        if not host:
            raise ValueError("target at index %d is missing 'host'" % index)
        stage = "canary" if index < canary_count else "promote"
        steps.append(RolloutPlanStep(node_id=node_id, host=host, stage=stage, status="planned"))

    return RolloutPlan(version=version.strip(), steps=steps)


# ---------------------------------------------------------------------------
# Rollout executor / simulator
# ---------------------------------------------------------------------------


def execute_staged_rollout(
    plan: RolloutPlan,
    *,
    deploy_fn: Callable[[RolloutPlanStep], bool],
    health_fn: Optional[Callable[[RolloutPlanStep], bool]] = None,
    simulate: bool = False,
) -> RolloutResult:
    """Execute (or simulate) a staged rollout following canary/promote logic.

    Canary phase:
      Each canary step is deployed in order.  After every successful canary
      deployment *health_fn* is called (when provided); a failing health check
      marks the step as ``"failed"`` and halts the rollout — all remaining
      steps are marked ``"skipped"``.

    Promote phase:
      Entered only when all canary steps succeed.  Each promote step is
      deployed in sequence; a deploy failure stops further promotion and marks
      remaining steps ``"skipped"``.

    Args:
        plan: The :class:`RolloutPlan` returned by
            :func:`build_staged_rollout_plan`.
        deploy_fn: Callable that accepts a :class:`RolloutPlanStep` and
            returns ``True`` on success.  When *simulate* is ``True`` this
            function is never called.
        health_fn: Optional callable that accepts a :class:`RolloutPlanStep`
            and returns ``True`` when the node is healthy.  Only invoked after
            canary steps.  When *simulate* is ``True`` this is never called.
        simulate: When ``True`` all steps are marked ``"succeeded"`` without
            calling *deploy_fn* or *health_fn*.

    Returns:
        A :class:`RolloutResult` summarising which nodes succeeded, failed,
        or were skipped.
    """
    result = RolloutResult(version=plan.version)
    halted = False

    for step in plan.steps:
        if halted:
            step.status = "skipped"
            result.skipped.append(step.node_id)
            continue

        step.status = "active"

        if simulate:
            step.status = "succeeded"
            result.succeeded.append(step.node_id)
            continue

        deploy_ok = deploy_fn(step)
        if not deploy_ok:
            step.status = "failed"
            result.failed.append(step.node_id)
            halted = True
            continue

        if step.stage == "canary" and health_fn is not None:
            healthy = health_fn(step)
            if not healthy:
                step.status = "failed"
                result.failed.append(step.node_id)
                halted = True
                continue

        step.status = "succeeded"
        result.succeeded.append(step.node_id)

    return result
