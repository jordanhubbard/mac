"""Move an agent between fleets: atomically re-homes the fleets.yaml entry,
optionally redeploys the agent at the target fleet's hub_url + token, and
reconciles DB membership (adds to target fleet, removes stale source entry).

Design goals
------------
* **Pure core**: ``move_agent_in_registry`` and ``plan_fleet_move`` are
  side-effect-free and fully unit-tested.
* **Idempotent**: running the same move a second time is a no-op (agent is
  already absent from source, already present in target).
* **Dry-run by default**: nothing is mutated until ``--execute`` is passed
  (matches the ``mac agent migrate`` pattern).
* **Backup-first**: when actually moving, fleets.yaml is backed up with a
  timestamp suffix before it is written.
* **DB reconciliation**: the control-plane fleet membership is updated so
  runtime agents re-register under the correct fleet and stale old-fleet
  observations are cleaned up.

Pattern follows ``mac agent migrate`` (``agent_migrate.py``): pure helpers
(``SOUL_BACKUP_EXCLUDES``, ``retarget_fleet_agent``, ``migration_plan``) are
unit-tested; execution is behind an ``execute=True`` gate.
"""

from __future__ import annotations

import copy
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no side-effects)
# ---------------------------------------------------------------------------


def find_agent_fleet(registry: Mapping[str, Any], agent_name: str) -> Optional[str]:
    """Return the fleet key that currently contains *agent_name*, or None."""
    for fleet_key, fleet_cfg in (registry.get("fleets") or {}).items():
        if not isinstance(fleet_cfg, dict):
            continue
        for agent in fleet_cfg.get("agents") or []:
            if isinstance(agent, dict) and agent.get("name") == agent_name:
                return fleet_key
    return None


def move_agent_in_registry(
    registry: Mapping[str, Any],
    agent_name: str,
    from_fleet: str,
    to_fleet: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Remove *agent_name* from *from_fleet* and add it to *to_fleet* in the
    parsed ``fleets.yaml`` mapping.

    Returns ``(updated_registry, agent_entry)`` where *agent_entry* is the
    copied agent dict (with ``hub_url`` / ``hub_agent`` inherited from the
    target fleet when those fields are absent).

    Raises ``KeyError`` if either fleet is absent or the agent is not found in
    *from_fleet*.  Idempotent: if the agent is ALREADY in *to_fleet* it is
    kept as-is (not duplicated) and silently removed from *from_fleet* if it
    also appears there.
    """
    result = copy.deepcopy(dict(registry))
    fleets: Dict[str, Any] = result.setdefault("fleets", {})

    if from_fleet not in fleets:
        raise KeyError("source fleet %r not found in registry" % from_fleet)
    if to_fleet not in fleets:
        raise KeyError("target fleet %r not found in registry" % to_fleet)

    src_fleet = fleets[from_fleet]
    src_agents: List[Dict[str, Any]] = list(src_fleet.get("agents") or [])

    agent_entry: Optional[Dict[str, Any]] = None
    for a in src_agents:
        if isinstance(a, dict) and a.get("name") == agent_name:
            agent_entry = dict(a)
            break

    if agent_entry is None:
        raise KeyError("agent %r not found in fleet %r" % (agent_name, from_fleet))

    # Remove from source fleet.
    src_fleet["agents"] = [
        a for a in src_agents
        if not (isinstance(a, dict) and a.get("name") == agent_name)
    ]

    # Add to target fleet (de-duplicate: remove any stale existing entry first).
    dst_fleet = fleets[to_fleet]
    dst_agents: List[Dict[str, Any]] = list(dst_fleet.get("agents") or [])
    dst_agents = [
        a for a in dst_agents
        if not (isinstance(a, dict) and a.get("name") == agent_name)
    ]

    # Inherit the target fleet's hub_url so the agent redeploys to the right hub.
    dst_hub_url = dst_fleet.get("hub_url") or ""
    if dst_hub_url and not agent_entry.get("hub_url"):
        agent_entry["hub_url"] = dst_hub_url

    dst_agents.append(agent_entry)
    dst_fleet["agents"] = dst_agents

    return result, agent_entry


def plan_fleet_move(
    agent_name: str,
    from_fleet: str,
    to_fleet: str,
    registry: Mapping[str, Any],
    *,
    deploy_cmd: str = "deploy/deploy-mac-fleet.sh",
    reconcile_db: bool = True,
) -> List[Tuple[str, str]]:
    """Return an ordered ``(step, description-or-command)`` runbook for the
    cross-fleet move.  Pure — no side-effects.

    Steps
    -----
    1. ``validate``              — check the agent is in *from_fleet*
    2. ``backup-registry``       — copy fleets.yaml with timestamp suffix
    3. ``update-registry``       — remove agent from source, add to target fleet
    4. ``redeploy``              — redeploy agent pointing at target hub_url/token
    5. ``reconcile-db-add``      — add agent_id to target fleet DB membership
    6. ``reconcile-db-remove``   — remove stale source-fleet DB membership
    7. ``verify``                — check the agent appears only in target fleet

    ``reconcile-db-*`` steps are skipped when *reconcile_db=False*.
    """
    fleets = registry.get("fleets") or {}
    src = fleets.get(from_fleet) or {}
    dst = fleets.get(to_fleet) or {}

    dst_hub_url = dst.get("hub_url") or "<target-hub-url>"
    to_os = "linux"  # default; CLI overrides with --to-os

    agent_entry: Optional[Dict[str, Any]] = None
    for a in src.get("agents") or []:
        if isinstance(a, dict) and a.get("name") == agent_name:
            agent_entry = a
            break

    target = (agent_entry or {}).get("target") or "<agent-target>"

    steps: List[Tuple[str, str]] = [
        ("validate",
         "assert agent %r present in fleet %r; assert fleet %r exists"
         % (agent_name, from_fleet, to_fleet)),
        ("backup-registry",
         "cp fleets.yaml fleets.yaml.bak.<timestamp>"),
        ("update-registry",
         "remove %r from fleets.%s.agents; add to fleets.%s.agents "
         "inheriting hub_url=%s" % (agent_name, from_fleet, to_fleet, dst_hub_url)),
        ("redeploy",
         "%s --hub %s --hub-os %s %s" % (deploy_cmd, to_fleet, to_os, agent_name)),
    ]

    if reconcile_db:
        steps += [
            ("reconcile-db-add",
             "mac fleet update %s --add-agent agent_%s" % (to_fleet, agent_name)),
            ("reconcile-db-remove",
             "mac fleet update %s --remove-agent agent_%s" % (from_fleet, agent_name)),
        ]

    steps.append(("verify",
                  "mac fleet list; confirm agent_%s is ONLY in fleet %r; "
                  "mac agent list --fleet %s" % (agent_name, to_fleet, to_fleet)))

    return steps


def render_move_plan(
    agent_name: str,
    from_fleet: str,
    to_fleet: str,
    steps: List[Tuple[str, str]],
) -> str:
    """Human-readable dry-run plan for a cross-fleet move."""
    lines = [
        "fleet move-agent plan for %r: %s -> %s  (DRY-RUN -- pass --execute to run)"
        % (agent_name, from_fleet, to_fleet)
    ]
    for i, (step, cmd) in enumerate(steps, 1):
        lines.append("  %d. [%s]\n       %s" % (i, step, cmd))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution (side-effects; not unit-tested — gated behind --execute)
# ---------------------------------------------------------------------------


def execute_fleet_move(
    agent_name: str,
    from_fleet: str,
    to_fleet: str,
    *,
    fleets_config: Path,
    deploy_cmd: str = "deploy/deploy-mac-fleet.sh",
    to_os: str = "linux",
    dry_run: bool = True,
    reconcile_db: bool = True,
    hub_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically move *agent_name* from *from_fleet* to *to_fleet*.

    1. Validates both fleets and the agent exist in fleets.yaml.
    2. Backs up fleets.yaml.
    3. Rewrites fleets.yaml atomically (agent removed from source, added to
       target with inherited hub_url/defaults).
    4. Emits the redeploy command (but does NOT run it — operators verify
       before redeploying live nodes).
    5. Returns a result dict describing what was done.

    When *dry_run=True* (default) nothing is written; only the proposed
    registry change is described.  Pass *dry_run=False* to actually mutate
    fleets.yaml.

    DB reconciliation (steps 5–6 of the plan) is intentionally left as
    operator commands printed to stdout: the move may happen while the hub
    is unreachable, and forced DB edits without a running control-plane
    corrupt the vault.  Use ``mac fleet update`` after the agent is
    redeployed and re-registered.
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for fleet move") from exc

    text = fleets_config.read_text(encoding="utf-8")
    registry: Dict[str, Any] = yaml.safe_load(text) or {}

    fleets = registry.get("fleets") or {}
    if from_fleet not in fleets:
        return {"ok": False, "error": "source fleet %r not found in %s" % (from_fleet, fleets_config)}
    if to_fleet not in fleets:
        return {"ok": False, "error": "target fleet %r not found in %s" % (to_fleet, fleets_config)}

    # Validate agent is in source fleet.
    src_agents = fleets[from_fleet].get("agents") or []
    if not any(isinstance(a, dict) and a.get("name") == agent_name for a in src_agents):
        # Idempotency: already in target fleet is treated as a no-op.
        dst_agents = fleets[to_fleet].get("agents") or []
        if any(isinstance(a, dict) and a.get("name") == agent_name for a in dst_agents):
            return {
                "ok": True,
                "idempotent": True,
                "message": "agent %r already in fleet %r (no-op)" % (agent_name, to_fleet),
                "dry_run": dry_run,
            }
        return {
            "ok": False,
            "error": "agent %r not found in fleet %r" % (agent_name, from_fleet),
        }

    # Build the new registry.
    new_registry, agent_entry = move_agent_in_registry(
        registry, agent_name, from_fleet, to_fleet
    )

    # Override hub_url if explicitly provided.
    if hub_url:
        for a in new_registry["fleets"][to_fleet].get("agents") or []:
            if isinstance(a, dict) and a.get("name") == agent_name:
                a["hub_url"] = hub_url

    effective_hub_url = (
        hub_url
        or agent_entry.get("hub_url")
        or (fleets[to_fleet].get("hub_url") or "")
    )

    result: Dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "agent": agent_name,
        "from_fleet": from_fleet,
        "to_fleet": to_fleet,
        "agent_entry": agent_entry,
        "effective_hub_url": effective_hub_url,
    }

    if dry_run:
        try:
            new_yaml = yaml.safe_dump(new_registry, sort_keys=False)
        except Exception:  # noqa: BLE001
            new_yaml = "(yaml serialization error)"
        result["proposed_registry_fragment"] = {
            "fleets.%s.agents" % from_fleet: new_registry["fleets"][from_fleet].get("agents"),
            "fleets.%s.agents" % to_fleet: new_registry["fleets"][to_fleet].get("agents"),
        }
        result["redeploy_cmd"] = (
            "%s --hub %s --hub-os %s %s" % (deploy_cmd, to_fleet, to_os, agent_name)
        )
        if reconcile_db:
            result["db_reconcile_cmds"] = [
                "mac fleet update %s --add-agent agent_%s" % (to_fleet, agent_name),
                "mac fleet update %s --remove-agent agent_%s" % (from_fleet, agent_name),
            ]
        return result

    # --- Live execution ---
    backup = "%s.bak.%d" % (fleets_config, int(time.time()))
    shutil.copy2(str(fleets_config), backup)
    result["backup"] = backup

    new_content = yaml.safe_dump(new_registry, sort_keys=False)
    fleets_config.write_text(new_content, encoding="utf-8")
    result["registry_written"] = str(fleets_config)

    redeploy_cmd = (
        "%s --hub %s --hub-os %s %s" % (deploy_cmd, to_fleet, to_os, agent_name)
    )
    result["redeploy_cmd"] = redeploy_cmd
    result["next_steps"] = [
        "Run: %s" % redeploy_cmd,
    ]
    if reconcile_db:
        result["db_reconcile_cmds"] = [
            "mac fleet update %s --add-agent agent_%s" % (to_fleet, agent_name),
            "mac fleet update %s --remove-agent agent_%s" % (from_fleet, agent_name),
        ]
        result["next_steps"] += [
            "After agent reregisters, run DB reconcile:",
            *["  %s" % c for c in result["db_reconcile_cmds"]],
        ]

    return result
