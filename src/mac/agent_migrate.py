"""First-class agent migration: move an agent (soul + memory) between hosts.

Codifies the manual playbook for relocating a fleet agent — e.g. moving
``hostd`` from a macOS host to a Linux host:

  1. back up the soul from the source host (selective: memory/state IN; host
     cruft, host-specific runtime config, and deploy-managed skills OUT),
  2. retarget the agent in the fleet topology (``fleets.yaml``),
  3. deploy to the destination host (the agent NAME is pinned, so the hub-stored
     persona/memories/mood follow ``agent_<name>`` automatically),
  4. restore the soul over the deploy's fresh home (keeping the deploy's
     host-correct ``config.yaml``/``.env``/skills),
  5. optionally decommission the source + retire its agent record,
  6. verify the soul transferred (``SOUL.md`` sha256 matches).

The pure helpers (``SOUL_BACKUP_EXCLUDES``, ``retarget_fleet_agent``,
``migration_plan``) are unit-tested. ``execute_migration`` shells out to
ssh/scp + the fleet deploy and is gated behind an explicit ``execute=True``.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# ~/.hermes entries that must NOT migrate with the soul:
#   * host-local cruft (old checkouts, logs, bins, caches)
#   * host-specific runtime config the deploy re-writes for the new host
#   * deploy-managed skills (the deploy installs fleet + per-host GPU skills)
SOUL_BACKUP_EXCLUDES: List[str] = [
    ".hermes/hermes-agent.old-feature-branch",
    ".hermes/logs",
    ".hermes/bin",
    ".hermes/cache",
    ".hermes/audio_cache",
    ".hermes/skills",
    ".hermes/config.yaml",
    ".hermes/config.yaml.*",
    ".hermes/.env",
    ".hermes/.env.*",
]

# Services to stop before swapping the live state.db, and restart after.
_FLEET_SERVICES = ["mac-agent", "mac-hermes-gateway", "mac", "mac-gen-server"]


def soul_backup_tar_excludes() -> List[str]:
    """``--exclude=`` args for the selective soul backup tar."""
    return ["--exclude=%s" % e for e in SOUL_BACKUP_EXCLUDES]


def retarget_fleet_agent(
    registry: Mapping[str, Any],
    fleet: str,
    name: str,
    *,
    target: str,
    os: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Point an agent at a new host in a parsed ``fleets.yaml`` mapping (mutates
    it in place). Returns the previous ``(target, os)``. Raises KeyError if the
    fleet or agent is absent."""
    try:
        agents = registry["fleets"][fleet]["agents"]
    except (KeyError, TypeError) as exc:
        raise KeyError("fleet %r not found in registry" % fleet) from exc
    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == name:
            previous = (agent.get("target"), agent.get("os"))
            agent["target"] = target
            if os is not None:
                agent["os"] = os
            return previous
    raise KeyError("agent %r not found in fleet %r" % (name, fleet))


def migration_plan(
    name: str,
    *,
    src_target: str,
    dst_target: str,
    fleet: str,
    deploy_cmd: str = "deploy/deploy-mac-fleet.sh",
    keep_source: bool = False,
    retire_source_agent: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Ordered ``(step, command)`` runbook for the migration. Pure — builds the
    exact shell the operator (or ``execute_migration``) runs, soul-clobber-safe."""
    tgz = "/tmp/%s-soul.tgz" % name
    excludes = " ".join(soul_backup_tar_excludes())
    svcs = " ".join(_FLEET_SERVICES)
    steps: List[Tuple[str, str]] = [
        ("backup-soul",
         "ssh %s 'cd \"$HOME\" && tar czf %s %s .hermes'" % (src_target, tgz, excludes)),
        ("transfer",
         "scp -3 %s:%s %s:%s" % (src_target, tgz, dst_target, tgz)),
        ("retarget-fleet",
         "(fleets.yaml) set agent %r target=%s" % (name, dst_target)),
        ("deploy",
         "%s --hub %s %s" % (deploy_cmd, fleet, name)),
        ("restore-soul",
         "ssh %s 'sudo systemctl stop %s; cd \"$HOME\" && tar xzf %s; "
         "sudo systemctl start %s'" % (dst_target, svcs, tgz, svcs)),
        ("verify",
         "compare sha256 ~/.hermes/SOUL.md on %s vs %s; mac agent list" % (src_target, dst_target)),
    ]
    if not keep_source:
        steps.append((
            "decommission-source",
            "ssh %s 'launchctl unload ~/Library/LaunchAgents/com.mac*.plist 2>/dev/null "
            "|| sudo systemctl stop %s'" % (src_target, svcs)))
    if retire_source_agent:
        steps.append(("retire-source-agent", "mac agent delete %s" % retire_source_agent))
    return steps


def render_plan(name: str, steps: List[Tuple[str, str]]) -> str:
    lines = ["migration plan for agent %r (DRY-RUN — pass --execute to run):" % name]
    for i, (step, cmd) in enumerate(steps, 1):
        lines.append("  %d. [%s]\n       %s" % (i, step, cmd))
    return "\n".join(lines)


def execute_migration(
    name: str,
    steps: List[Tuple[str, str]],
    *,
    runner: Callable[[str], int] = None,
) -> Dict[str, Any]:
    """Run the migration runbook step by step, stopping on the first failure.

    ``runner(command) -> returncode`` is injectable for testing; the default
    runs each command through the shell. Returns a result dict with per-step
    status. The ``retarget-fleet`` step is a marker handled by the CLI (it edits
    fleets.yaml in-process), not a shell command.
    """
    if runner is None:
        def runner(cmd: str) -> int:
            return subprocess.run(cmd, shell=True).returncode  # noqa: S602 — operator-invoked

    results: List[Dict[str, Any]] = []
    for step, cmd in steps:
        if step == "retarget-fleet":
            results.append({"step": step, "status": "handled-in-process"})
            continue
        rc = runner(cmd)
        results.append({"step": step, "command": cmd, "returncode": rc})
        if rc != 0:
            return {"agent": name, "ok": False, "failed_step": step, "results": results}
    return {"agent": name, "ok": True, "results": results}
