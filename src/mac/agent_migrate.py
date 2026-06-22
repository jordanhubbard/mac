"""First-class agent migration: move an agent and ALL its state between hosts.

Two fidelity levels, selected by whether the agent owns the durable hub state:

SPOKE (``hub=False``) — soul-only. A spoke's DB rows (persona/memories/mood) and
Qdrant vectors live in the SHARED hub and stay put; only the host-local soul
moves, and the deploy re-attaches ``agent_<name>`` to its existing hub-stored
state. Steps: back up the soul (memory/state IN; cruft, host-specific runtime
config, deploy-managed skills, and large regenerable caches OUT) -> retarget
``fleets.yaml`` -> deploy (NAME pinned) -> restore soul over the fresh home ->
verify (``SOUL.md`` sha256) -> decommission source.

HUB (``hub=True``) — full fidelity. The agent also hosts the durable hub state,
so we additionally move: the entire ``mac.db`` (tasks/projects/personas/
memories/messages/vault = the group hub memory), the Qdrant vector store
(per-agent + shared memory vectors), and the env-pinned hub secrets
(``MAC_SECRET_KEY``/``MAC_API_TOKEN``) without which the encrypted DB is
undecryptable. The migrated state is STAGED on the destination BEFORE the deploy
so the control-plane + gateway come up against the real vault (persona/identity
preserved, gateway self-test passes), not a fresh empty DB. This is the
authoritative path for relocating the hub itself (e.g. rocky -> a new host).

The pure helpers (``SOUL_BACKUP_EXCLUDES``, ``retarget_fleet_agent``,
``migration_plan``, the command builders) are unit-tested. ``execute_migration``
shells out to ssh/scp + the fleet deploy and is gated behind ``execute=True``.
The CLI auto-detects ``hub`` from the fleet's ``hub_agent`` /
``shared_services_manager_agent`` (override with ``--hub`` / ``--no-hub``).
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# ~/.hermes entries that must NOT migrate with the soul. The rule: keep
# everything that is personality OR memory; drop only host-local cruft,
# host-specific runtime config the deploy re-writes, deploy-managed skills, and
# large regenerable caches (which would otherwise bloat the transfer to GBs).
# Personality/memory KEPT explicitly: SOUL.md, memories/ (MEMORY.md, USER.md),
# sessions/ (conversation history), pastes/, scripts/, cron/, pairing/,
# platforms/, hooks/.
SOUL_BACKUP_EXCLUDES: List[str] = [
    ".hermes/hermes-agent.old-feature-branch",
    ".hermes/logs",
    ".hermes/bin",
    ".hermes/cache",
    ".hermes/audio_cache",
    ".hermes/image_cache",   # regenerable thumbnail/image cache
    ".hermes/lsp",           # language-server caches (often 100s of MB)
    ".hermes/sandboxes",     # per-task worktrees, regenerated on demand
    ".hermes/skills",
    ".hermes/config.yaml",
    ".hermes/config.yaml.*",
    ".hermes/.env",
    ".hermes/.env.*",
]

# Default on-disk locations of the hub's durable state. Overridable per call so
# the tool works regardless of how a host was provisioned.
DEFAULT_DB_PATH = "~/.mac/mac.db"
DEFAULT_ENV_PATH = "~/.mac/mac.env"
# Qdrant storage dir differs by platform: the Linux installer uses
# /var/lib/<fleet>/qdrant (root-owned); the macOS installer uses
# $MAC_HOME/qdrant (user-owned, mounted into the Docker Desktop container).
DEFAULT_QDRANT_DIR_LINUX = "/var/lib/{fleet_name}/qdrant"
DEFAULT_QDRANT_DIR_DARWIN = "~/.mac/qdrant"
# Secrets that are environment-pinned (not in the DB) and MUST move with the hub
# DB or the migrated, encrypted state becomes undecryptable / unauthenticated.
HUB_ENV_SECRETS = ["MAC_SECRET_KEY", "MAC_API_TOKEN"]

# Services to stop before swapping the live state.db, and restart after.
_FLEET_SERVICES = ["mac-agent", "mac-hermes-gateway", "mac", "mac-gen-server"]


def _qdrant_dir(os_kind: str, fleet_name: str) -> str:
    return (DEFAULT_QDRANT_DIR_DARWIN if os_kind == "darwin"
            else DEFAULT_QDRANT_DIR_LINUX.format(fleet_name=fleet_name))


def _control_plane_services(os_kind: str, fleet_name: str, *, include_qdrant: bool) -> List[str]:
    """Service identifiers (systemd unit names or launchd labels) for the hub's
    DB-touching services, in stop order."""
    if os_kind == "darwin":
        svcs = ["com.%s.agent" % fleet_name, "com.%s.hermes-gateway" % fleet_name,
                "com.%s.control-plane" % fleet_name]
        if include_qdrant:
            svcs.append("com.%s.qdrant" % fleet_name)
        return svcs
    svcs = ["mac-agent", "mac-hermes-gateway", "mac"]
    if include_qdrant:
        svcs.append("%s-qdrant" % fleet_name)
    return svcs


def _stop_services_cmd(target: str, os_kind: str, services: List[str]) -> str:
    if os_kind == "darwin":
        labels = " ".join(services)
        return ("ssh %s 'uid=$(id -u); for L in %s; do "
                "launchctl bootout gui/$uid/$L 2>/dev/null || true; done'"
                % (target, labels))
    return "ssh %s 'sudo systemctl stop %s'" % (target, " ".join(services))


def _restart_services_cmd(target: str, os_kind: str, fleet_name: str, services: List[str]) -> str:
    if os_kind == "darwin":
        la = "$HOME/Library/LaunchAgents"
        # bootstrap re-adds (RunAtLoad starts); kickstart -k as a fallback.
        inner = ("uid=$(id -u); for L in %s; do "
                 "launchctl bootstrap gui/$uid \"%s/$L.plist\" 2>/dev/null "
                 "|| launchctl kickstart -k gui/$uid/$L 2>/dev/null || true; done"
                 % (" ".join(reversed(services)), la))
        return "ssh %s '%s'" % (target, inner)
    return "ssh %s 'sudo systemctl start %s'" % (target, " ".join(reversed(services)))


def _seed_hub_secrets_cmd(src_target: str, dst_target: str, env_path: str) -> str:
    """Copy the environment-pinned hub secrets (MAC_SECRET_KEY, MAC_API_TOKEN)
    from src mac.env into dst mac.env BEFORE the deploy, so deploy_env preserves
    them and the migrated DB decrypts. Idempotent upsert; never prints values."""
    keys = "|".join(HUB_ENV_SECRETS)
    # dst-side python upserts each KEY=VALUE line read from stdin.
    upsert = (
        "import sys,os,re,pathlib;"
        "p=pathlib.Path(os.path.expanduser(%r));"
        "p.parent.mkdir(parents=True,exist_ok=True);"
        "lines=p.read_text().splitlines() if p.exists() else [];"
        "incoming=[l for l in sys.stdin.read().splitlines() if l.strip() and '=' in l];"
        "keys={l.split('=',1)[0] for l in incoming};"
        "kept=[l for l in lines if l.split('=',1)[0] not in keys] if lines else [];"
        "p.write_text(chr(10).join(kept+incoming)+chr(10));"
        "os.chmod(p,0o600)" % env_path
    )
    return ("ssh %s 'grep -E \"^(%s)=\" %s' | ssh %s 'python3 -c %s'"
            % (src_target, keys, env_path, dst_target, shlex.quote(upsert)))


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


def _launchd_labels(fleet_name: str) -> List[str]:
    """The launchd labels the deploy installs on a darwin host, in the order
    deploy-mac-fleet.sh uses (control-plane / hermes-gateway / agent). The deploy
    keys these on the fleet's ``fleet_name`` field, not the registry key."""
    return [
        "com.%s.control-plane" % fleet_name,
        "com.%s.hermes-gateway" % fleet_name,
        "com.%s.agent" % fleet_name,
    ]


def _darwin_restore_cmd(dst_target: str, tgz: str, fleet_name: str) -> str:
    """launchctl-based restore-soul for a darwin destination: bootout the agents,
    unpack the soul tar, kickstart them back. Mirrors deploy-mac-fleet.sh's
    gui/$uid/<label> bootout+kickstart shapes."""
    labels = " ".join(_launchd_labels(fleet_name))
    return (
        "ssh %s 'uid=$(id -u); "
        "for L in %s; do launchctl bootout gui/$uid/$L 2>/dev/null || true; done; "
        "cd \"$HOME\" && tar xzf %s; "
        "for L in %s; do launchctl kickstart -k gui/$uid/$L 2>/dev/null || true; done'"
        % (dst_target, labels, tgz, labels)
    )


def migration_plan(
    name: str,
    *,
    src_target: str,
    dst_target: str,
    fleet: str,
    fleet_name: Optional[str] = None,
    to_os: str = "linux",
    src_os: str = "linux",
    deploy_cmd: str = "deploy/deploy-mac-fleet.sh",
    keep_source: bool = False,
    retire_source_agent: Optional[str] = None,
    hub: bool = False,
    db_path: str = DEFAULT_DB_PATH,
    env_path: str = DEFAULT_ENV_PATH,
    src_qdrant_dir: Optional[str] = None,
    dst_qdrant_dir: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """Ordered ``(step, command)`` runbook for the migration. Pure — builds the
    exact shell the operator (or ``execute_migration``) runs, soul-clobber-safe.

    ``to_os``/``src_os`` select the destination/source service manager:
    ``linux`` uses systemd; ``darwin`` uses launchctl (the deploy installs
    ``com.<fleet_name>.*`` launchd agents). ``fleet_name`` defaults to ``fleet``
    when not given (the registry key and fleet_name usually match).

    ``hub=True`` performs a FULL-FIDELITY HUB migration: the agent being moved
    also hosts the durable hub state, so beyond the soul we move the entire
    ``mac.db`` (tasks/projects/personas/memories/messages/vault — the group hub
    memory), the Qdrant vector store (per-agent + shared memory vectors), and the
    environment-pinned hub secrets (``MAC_SECRET_KEY``/``MAC_API_TOKEN``) without
    which the migrated, encrypted DB is undecryptable. The migrated state is
    STAGED on the destination *before* the deploy, so the deploy's control-plane
    + gateway come up against the real vault (identity/persona preserved, gateway
    self-test passes) instead of a fresh empty DB.

    ``hub=False`` (spoke) keeps the historical soul-only flow: a spoke's DB rows
    and Qdrant vectors live in the shared hub and stay put; only the soul (which
    is host-local) moves, and the deploy re-attaches ``agent_<name>`` to its
    existing hub-stored persona/memories/mood."""
    fn = (fleet_name or fleet)
    tgz = "/tmp/%s-soul.tgz" % name
    db_tgz = "/tmp/%s-mac.db" % name
    qd_tgz = "/tmp/%s-qdrant.tgz" % name
    excludes = " ".join(soul_backup_tar_excludes())
    svcs = " ".join(_FLEET_SERVICES)
    sq = src_qdrant_dir or _qdrant_dir(src_os, fn)
    dq = dst_qdrant_dir or _qdrant_dir(to_os, fn)
    if to_os == "darwin":
        restore_cmd = _darwin_restore_cmd(dst_target, tgz, fn)
    else:
        restore_cmd = (
            "ssh %s 'sudo systemctl stop %s; cd \"$HOME\" && tar xzf %s; "
            "sudo systemctl start %s'" % (dst_target, svcs, tgz, svcs))

    steps: List[Tuple[str, str]] = []

    if hub:
        # 1. Quiesce the source hub so the DB + Qdrant snapshot is consistent.
        src_svcs = _control_plane_services(src_os, fn, include_qdrant=True)
        steps.append(("stop-source-hub", _stop_services_cmd(src_target, src_os, src_svcs)))
        # 2. Consistent online DB backup (works without the sqlite3 CLI).
        db_backup = (
            "import sqlite3,os;"
            "s=sqlite3.connect(os.path.expanduser(%r));"
            "d=sqlite3.connect(%r);"
            "s.backup(d);d.close();s.close()" % (db_path, db_tgz))
        steps.append(("backup-db-source",
                      "ssh %s 'python3 -c %s'" % (src_target, shlex.quote(db_backup))))
        # 3. Qdrant storage snapshot (Linux /var/lib needs sudo + chown for scp).
        if src_os == "darwin":
            qd_cmd = "tar czf %s -C %s ." % (qd_tgz, sq)
        else:
            qd_cmd = ("sudo tar czf %s -C %s . && sudo chown \"$USER\" %s"
                      % (qd_tgz, sq, qd_tgz))
        steps.append(("backup-qdrant-source", "ssh %s '%s'" % (src_target, qd_cmd)))

    # 4. Soul backup (always).
    steps.append(("backup-soul",
                  "ssh %s 'cd \"$HOME\" && tar czf %s %s .hermes'" % (src_target, tgz, excludes)))

    # 5. Transfer artifacts src -> dst.
    steps.append(("transfer-soul", "scp -3 %s:%s %s:%s" % (src_target, tgz, dst_target, tgz)))
    if hub:
        steps.append(("transfer-db", "scp -3 %s:%s %s:%s" % (src_target, db_tgz, dst_target, db_tgz)))
        steps.append(("transfer-qdrant", "scp -3 %s:%s %s:%s" % (src_target, qd_tgz, dst_target, qd_tgz)))
        # 6. Seed the env-pinned secrets BEFORE deploy so deploy_env preserves
        #    them and the migrated DB decrypts.
        steps.append(("seed-hub-secrets", _seed_hub_secrets_cmd(src_target, dst_target, env_path)))
        # 7. Stage DB + Qdrant on dst (stop any running dst hub services first).
        dst_svcs = _control_plane_services(to_os, fn, include_qdrant=True)
        steps.append(("stage-stop-dest", _stop_services_cmd(dst_target, to_os, dst_svcs)))
        steps.append(("stage-db-dest",
                      "ssh %s 'mkdir -p \"$(dirname %s)\"; rm -f %s-wal %s-shm; mv -f %s %s'"
                      % (dst_target, db_path, db_path, db_path, db_tgz, db_path)))
        steps.append(("stage-qdrant-dest",
                      "ssh %s 'mkdir -p %s && rm -rf %s/* && tar xzf %s -C %s'"
                      % (dst_target, dq, dq, qd_tgz, dq)))

    # 8. Retarget + deploy (deploy comes up against the staged hub state).
    steps.append(("retarget-fleet", "(fleets.yaml) set agent %r target=%s" % (name, dst_target)))
    steps.append(("deploy", "%s --hub %s --hub-os %s %s" % (deploy_cmd, fleet, to_os, name)))

    # 9. Restore soul over the deploy's fresh ~/.hermes, then verify.
    steps.append(("restore-soul", restore_cmd))
    if hub:
        verify = ("compare sha256 ~/.hermes/SOUL.md on %s vs %s; "
                  "mac agent list (decrypts); "
                  "row counts match src for agents/personas/memory_records/messages; "
                  "qdrant /collections vector counts match" % (src_target, dst_target))
    else:
        verify = "compare sha256 ~/.hermes/SOUL.md on %s vs %s; mac agent list" % (src_target, dst_target)
    steps.append(("verify", verify))

    if not keep_source:
        if src_os == "darwin":
            labels = " ".join(_launchd_labels(fn))
            decommission = (
                "ssh %s 'uid=$(id -u); for L in %s; do "
                "launchctl bootout gui/$uid/$L 2>/dev/null || true; done'" % (src_target, labels))
        else:
            decommission = "ssh %s 'sudo systemctl stop %s'" % (src_target, svcs)
        steps.append(("decommission-source", decommission))
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
