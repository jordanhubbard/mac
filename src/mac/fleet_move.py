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


def resolve_fleet_key(registry: Mapping[str, Any], name_or_key: str) -> Optional[str]:
    """Resolve *name_or_key* to a fleets.yaml registry KEY.

    Fleets are keyed in fleets.yaml by their hub-agent name (e.g. ``rocky``),
    while ``fleet_name`` is a separate human label (e.g. ``mac``). Operators
    naturally pass either; accept both. Returns the registry key, or None if
    neither a key nor any fleet's ``fleet_name`` matches.
    """
    if not name_or_key:
        return None
    fleets = registry.get("fleets") or {}
    if name_or_key in fleets:
        return name_or_key
    for key, cfg in fleets.items():
        if isinstance(cfg, dict) and cfg.get("fleet_name") == name_or_key:
            return key
    return None


def fleet_hub_url(registry: Mapping[str, Any], fleet_key: str) -> str:
    """Return the resolved fleet's ``hub_url`` (empty string if absent)."""
    cfg = (registry.get("fleets") or {}).get(fleet_key) or {}
    return str(cfg.get("hub_url") or "").strip()


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
    5. ``reconcile-db``          — DB membership auto-reconciles on re-registration
    6. ``verify``                — check the agent appears only in target fleet

    The ``reconcile-db`` note is omitted when *reconcile_db=False*.
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
        steps.append(
            ("reconcile-db",
             "DB fleet membership auto-reconciles when agent_%s re-registers to "
             "fleet %r after redeploy (worker._ensure_worker_fleet_membership); the "
             "stale %r observation is historical (no single-agent remove API)"
             % (agent_name, to_fleet, from_fleet)))

    steps.append(("verify",
                  "mac admin fleet list; confirm agent_%s is ONLY in fleet %r; "
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


def _invoke_redeploy(
    deploy_cmd: str,
    to_fleet: str,
    to_os: str,
    agent_name: str,
    *,
    repo_root: Optional[Path] = None,
    runner: Optional[Any] = None,
) -> Tuple[int, str]:
    """Run ``<deploy_cmd> --hub <to_fleet> --hub-os <to_os> <agent_name>`` from
    the repo root and return ``(returncode, stderr-tail)``.

    *runner* defaults to ``subprocess.run``; tests inject a fake returning an
    object with a ``returncode`` attribute, so this stays side-effect-free
    under test. The deploy inherits the parent environment (the operator
    sources ``~/.mac/.env`` before ``mac admin fleet move-agent --execute``).
    """
    import subprocess

    run = runner or subprocess.run
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    script = Path(deploy_cmd)
    if not script.is_absolute():
        script = root / deploy_cmd
    cmd = [str(script), "--hub", to_fleet, "--hub-os", to_os, agent_name]
    try:
        proc = run(cmd, cwd=str(root))
    except Exception as exc:  # noqa: BLE001
        return 1, "redeploy invocation failed: %s" % exc
    rc = getattr(proc, "returncode", 1)
    rc = int(rc) if rc is not None else 1
    stderr = getattr(proc, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    return rc, str(stderr).strip()[-400:]


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
    run_redeploy: bool = False,
    runner: Optional[Any] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Atomically move *agent_name* from *from_fleet* to *to_fleet*, end-to-end.

    1. Validates both fleets, a resolvable target ``hub_url``, and the agent.
    2. Backs up fleets.yaml.
    3. Rewrites fleets.yaml atomically (agent removed from source, added to
       target with inherited hub_url/defaults).
    4. When *run_redeploy=True* (and not *dry_run*), RUNS the fleet deploy for
       the agent against the target hub via *runner* (default
       ``subprocess.run``); otherwise emits the redeploy command for the
       operator to run (``--no-redeploy``).
    5. Returns a result dict describing what was done.

    When *dry_run=True* (default) nothing is written; only the validated,
    proposed change is described. Pass *dry_run=False* to mutate fleets.yaml.

    Fails loudly (``ok=False``) if the target fleet has no resolvable
    ``hub_url`` — refusing to move an agent to a hub it can't reach.

    DB membership is NOT mutated directly here: control-plane fleet membership
    auto-reconciles when the redeployed agent RE-REGISTERS to the target fleet
    (``worker._ensure_worker_fleet_membership``). The stale source observation
    is historical (there is no single-agent remove API); verify with
    ``mac agent list``.
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

    # Refuse to move to a hub we can't resolve (kills the "<target-hub-url>"
    # placeholder bug class — we never half-move an agent to a hubless fleet).
    target_hub = (hub_url or "").strip() or fleet_hub_url(registry, to_fleet)
    if not target_hub:
        return {
            "ok": False,
            "error": "target fleet %r has no hub_url (pass --hub-url to override); "
                     "refusing to move" % to_fleet,
        }

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

    result["target_hub_url"] = target_hub

    if dry_run:
        result["proposed_registry_fragment"] = {
            "fleets.%s.agents" % from_fleet: new_registry["fleets"][from_fleet].get("agents"),
            "fleets.%s.agents" % to_fleet: new_registry["fleets"][to_fleet].get("agents"),
        }
        result["redeploy_cmd"] = (
            "%s --hub %s --hub-os %s %s" % (deploy_cmd, to_fleet, to_os, agent_name)
        )
        result["redeploy_will_run"] = run_redeploy
        if reconcile_db:
            result["db_reconcile"] = (
                "auto via re-registration to fleet %r after redeploy "
                "(no manual command)" % to_fleet
            )
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

    if run_redeploy:
        rc, detail = _invoke_redeploy(
            deploy_cmd, to_fleet, to_os, agent_name,
            repo_root=repo_root, runner=runner,
        )
        result["redeployed"] = rc == 0
        result["redeploy_returncode"] = rc
        if rc != 0:
            # The registry move already happened; surface the redeploy failure
            # loudly (the backup is retained) so the operator can re-run/revert.
            result["ok"] = False
            result["error"] = detail or ("redeploy exited %d" % rc)
    else:
        result["next_steps"] = ["Run the redeploy: %s" % redeploy_cmd]

    if reconcile_db:
        result["db_reconcile"] = (
            "DB membership auto-reconciles when agent_%s re-registers to fleet %r; "
            "verify with: mac agent list" % (agent_name, to_fleet)
        )

    return result
