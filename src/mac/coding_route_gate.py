"""The coding-route gate: may this worker take a repository coding task?

A worker proves it can run a coding CLI by probing the route from inside its
sandbox and reporting the result in ``resources.coding_clis`` (schema
``mac.coding_clis.v2``). With ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT`` on,
the hub hands repository work only to a worker with a fresh proof for at least
one CLI on the hub's ordered list (``MAC_CODING_AGENTS``).

Every assignment path asks this module, and only this module:

* the allocator's pair evaluation (``mac.allocator.evaluate_pair``), which
  serves the global allocation round, ``dispatch_once`` and dispatch batches,
  pull claims, targeted claims, retry-exclusion relaxation and dispatch
  explanations; and
* the transactional claim boundary (``ControlPlane.claim_task``), under both
  the legacy and the allocator-v2 rechecks.

They used to disagree: the claim boundary checked the proof on the legacy path
only, and the allocator never did, so ``dispatch_once`` assigned repository
work to workers whose only proof was for a CLI off the list, or that had no
fresh proof at all.

The decision is split in two so the allocator can stay a pure function over a
snapshot: :func:`proof_from_resources` summarizes what one agent reported, and
:func:`refusal` decides one task against that summary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, FrozenSet, Iterable, Mapping, Optional

STRICT_ENV = "MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT"
MAX_AGE_ENV = "MAC_CODING_ROUTE_MAX_AGE_SECONDS"
DEFAULT_MAX_AGE_SECONDS = 1200.0
REPORT_SCHEMA = "mac.coding_clis.v2"

#: The worker reports no v2 route proof at all (an old worker, or one that has
#: not heartbeated since it started).
ROUTE_UNREPORTED = "coding_agent_route_unreported"
#: No CLI on the hub's list has a fresh, exact proof.
ROUTE_UNVERIFIED = "coding_agent_route_unverified"
#: A listed CLI is verified, but not for the model this task pins.
MODEL_UNVERIFIED = "coding_agent_model_unverified"

REFUSALS: FrozenSet[str] = frozenset({ROUTE_UNREPORTED, ROUTE_UNVERIFIED, MODEL_UNVERIFIED})


@dataclass(frozen=True)
class CodingRouteProof:
    """What one agent has proven about its coding routes, for the gate.

    The default is an agent the gate does not apply to, so snapshots built
    outside the hub (tests, capacity probes) stay permissive.
    """

    #: The gate applies: strict mode is on and the agent must run tasks in
    #: OpenShell. Hub-side stand-ins and unsandboxed hosts are not gated here.
    required: bool = False
    #: The worker sent a v2 route report.
    reported: bool = False
    #: At least one listed CLI has a fresh proof whose fingerprint matches.
    fresh: bool = False
    #: Models proven by those fresh, listed CLIs.
    fresh_models: FrozenSet[str] = field(default_factory=frozenset)


def strict_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(STRICT_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def max_age_seconds(env: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if env is None else env
    try:
        return max(1.0, float(env.get(MAX_AGE_ENV) or DEFAULT_MAX_AGE_SECONDS))
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def item_is_fresh(item: Mapping[str, Any], *, now: datetime, max_age: float) -> bool:
    """Whether one CLI's report is a fresh, exact, in-sandbox proof."""
    if not (item.get("configured") is True and item.get("verified") is True):
        return False
    verification = item.get("verification")
    if not isinstance(verification, Mapping):
        return False
    checked_at = str(verification.get("checked_at") or "").strip()
    try:
        age = (now - _parse_time(checked_at)).total_seconds()
    except Exception:  # noqa: BLE001 - a malformed proof fails closed.
        return False
    if age < 0 or age > max_age:
        return False
    return verification.get("route_fingerprint") == item.get("route_fingerprint")


def item_models(item: Mapping[str, Any]) -> FrozenSet[str]:
    """The models one CLI's proof covers."""
    verification = item.get("verification")
    verification = verification if isinstance(verification, Mapping) else {}
    models = {
        str(value).strip()
        for value in (verification.get("verified_models") or [])
        if str(value).strip()
    }
    primary = str(verification.get("model") or item.get("model") or "").strip()
    if primary:
        models.add(primary)
    return frozenset(models)


def proof_from_resources(
    resources: Mapping[str, Any],
    *,
    requires_openshell: bool,
    listed_agents: Iterable[str],
    now: Optional[datetime] = None,
    env: Optional[Mapping[str, str]] = None,
) -> CodingRouteProof:
    """Summarize an agent's ``coding_clis`` report against the hub's list.

    CLIs off the list are ignored: a proof for a CLI the hub will not run
    cannot make a worker eligible.
    """
    if not (requires_openshell and strict_enabled(env)):
        return CodingRouteProof()
    coding = resources.get("coding_clis")
    if not isinstance(coding, Mapping) or coding.get("schema") != REPORT_SCHEMA:
        return CodingRouteProof(required=True)
    clis = coding.get("clis")
    clis = clis if isinstance(clis, Mapping) else {}
    moment = now or datetime.now(timezone.utc)
    max_age = max_age_seconds(env)
    fresh = False
    models: set[str] = set()
    for name in listed_agents:
        item = clis.get(name)
        if isinstance(item, Mapping) and item_is_fresh(item, now=moment, max_age=max_age):
            fresh = True
            models.update(item_models(item))
    return CodingRouteProof(
        required=True, reported=True, fresh=fresh, fresh_models=frozenset(models)
    )


def refusal(
    proof: CodingRouteProof,
    *,
    task_requires_coding: bool,
    pinned_model: str = "",
) -> Optional[str]:
    """Why this agent may not take this task, or ``None`` when it may.

    Only repository coding tasks need a route; reports, answers and other
    non-repository work are exempt, as are agents the gate does not apply to.
    """
    if not (task_requires_coding and proof.required):
        return None
    if not proof.reported:
        return ROUTE_UNREPORTED
    if not proof.fresh:
        return ROUTE_UNVERIFIED
    pinned = str(pinned_model or "").strip()
    if pinned and pinned not in proof.fresh_models:
        return MODEL_UNVERIFIED
    return None
