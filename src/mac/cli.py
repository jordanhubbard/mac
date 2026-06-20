from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from mac.migration import import_jsonl, migrate_acc_sqlite
from mac.models import MACError
from mac.services import ControlPlane
from mac.store import SQLiteStore


def _json_arg(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _csv(value: Optional[str]) -> Iterable[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _read_text_arg(
    inline: Optional[str],
    file_path: Optional[str],
    *,
    label: str,
    default: str = "",
) -> str:
    """Resolve a text-bearing CLI argument from one of: inline string,
    file path, or '-' for stdin. Lets callers avoid shell-quoting
    hazards on multi-line / metacharacter-heavy values (parens, braces,
    backticks, ``$``).

    Inline value wins when both are supplied. ``--<arg>-file -`` reads
    from stdin so pipes and heredocs work without quoting.
    """
    import sys
    if inline is not None and inline != "":
        return inline
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise SystemExit("failed to read %s from %s: %s" % (label, file_path, exc))
    return default


def _read_json_arg(
    inline: Optional[str],
    file_path: Optional[str],
    *,
    label: str,
    default: Any,
) -> Any:
    raw = _read_text_arg(inline, file_path, label=label, default="")
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("invalid JSON in %s: %s" % (label, exc))


def _print(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()

    def _to_serializable(obj: Any) -> Any:
        # Hub-mode handlers return _Dictish wrappers; a top-level one is
        # unwrapped above, but list/nested results (e.g. `project list` ->
        # list[_Dictish]) reach json.dumps un-unwrapped. Unwrap anything that
        # exposes the .to_dict() contract here so every command serializes.
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        raise TypeError(
            "Object of type %s is not JSON serializable" % type(obj).__name__
        )

    print(json.dumps(value, indent=2, sort_keys=True, default=_to_serializable))


def _plane(args: argparse.Namespace) -> Any:
    """Return a Dispatch (LocalDispatch or RemoteDispatch).

    Kept under the historical name so existing handlers (``_plane(args).foo()``)
    are unchanged. The actual transport is chosen by
    :func:`mac.dispatch.resolve_dispatch`.
    """
    from mac.dispatch import resolve_dispatch

    return resolve_dispatch(args)


def cmd_init(args: argparse.Namespace) -> None:
    _plane(args)
    _print({"status": "initialized", "db": args.db})


def cmd_config_migrate_env_namespace(args: argparse.Namespace) -> None:
    """mac-g55y: rewrite a flat ~/.mac/.env into fleet-scoped form.

    For every credential that may collide across fleets (MAC_API_TOKEN
    et al.), this appends a fleet-scoped sibling key like
    ``MAC_API_TOKEN__<FLEET>`` with the same value. The legacy flat
    key is preserved unless ``--drop-legacy`` is set, so other
    consumers continue working during the transition.
    """
    from pathlib import Path as _P

    from mac.fleet_env import migrate_env_file

    path = _P(args.env_file).expanduser()
    added, kept = migrate_env_file(path, args.fleet, keep_legacy=not args.drop_legacy)
    _print(
        {
            "env_file": str(path),
            "fleet": args.fleet,
            "added": sorted(added.keys()),
            "kept_legacy": sorted(kept.keys()) if not args.drop_legacy else [],
        }
    )


def cmd_fleet_sync_token(args: argparse.Namespace) -> None:
    """auth-token-sync-01: pull the hub's current bearer token into this client.

    The hub accepts only the tokens in its own ~/.mac/mac.env; the client sends
    MAC_API_TOKEN__<FLEET>. When they drift the hub returns 403 "unknown bearer
    token". This re-syncs the client from the authoritative source (the hub host,
    reached out-of-band over SSH).
    """
    from mac.fleet_creds import sync_token

    _print(
        sync_token(
            args.fleet,
            fleets_config_path=args.fleets_config,
            env_path=args.env_file,
        )
    )


def cmd_fleet_rotate_token(args: argparse.Namespace) -> None:
    """auth-token-sync-01: graceful bearer-token rotation via MAC_API_TOKENS.

    Default is a dry-run plan. --apply adds a new token alongside the old
    (overlap window) and advertises it as the new primary; --prune --apply
    drops the old tokens once every client has rolled over via sync-token.
    """
    from mac.fleet_creds import rotate_token

    _print(
        rotate_token(
            args.fleet,
            scopes=tuple(args.scope) if args.scope else ("admin",),
            prune=args.prune,
            do_apply=args.apply,
            restart=args.restart,
            fleets_config_path=args.fleets_config,
            env_path=args.env_file,
        )
    )


def cmd_tenant_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_tenant(
            args.name,
            metadata=_json_arg(args.metadata, {}),
            tenant_id=args.tenant_id,
        )
    )


def cmd_tenant_list(args: argparse.Namespace) -> None:
    _print([tenant.to_dict() for tenant in _plane(args).list_tenants()])


def cmd_user_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_user(
            args.tenant_id,
            args.handle,
            display_name=args.display_name or "",
            metadata=_json_arg(args.metadata, {}),
            user_id=args.user_id,
        )
    )


def cmd_persona_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_persona(
            args.tenant_id,
            args.name,
            args.soul_ref,
            args.memory_scope,
            metadata=_json_arg(args.metadata, {}),
            persona_id=args.persona_id,
        )
    )


def cmd_hermes_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_hermes_instance(
            args.tenant_id,
            args.name,
            persona_id=args.persona_id,
            home_ref=args.home_ref or "",
            status=args.status,
            metadata=_json_arg(args.metadata, {}),
            instance_id=args.instance_id,
        )
    )


def cmd_hermes_context(args: argparse.Namespace) -> None:
    _print(_plane(args).hermes_context(args.instance_id))


def cmd_hermes_work_context(args: argparse.Namespace) -> None:
    _print(
        _plane(args).hermes_work_context(
            args.instance_id,
            include_completed=not args.active_only,
            task_limit=args.task_limit,
        )
    )


def cmd_hermes_runtime_proof(args: argparse.Namespace) -> None:
    startup = None
    if not args.skip_startup_report:
        from mac.hermes_startup import build_hermes_startup_report

        startup = build_hermes_startup_report()
    _print(_plane(args).hermes_runtime_proof(args.instance_id, hermes_startup=startup))


def cmd_binding_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_platform_binding(
            args.tenant_id,
            args.hermes_instance_id,
            args.platform,
            args.external_id,
            display_name=args.display_name or "",
            scopes=_json_arg(args.scopes, {}),
            metadata=_json_arg(args.metadata, {}),
            binding_id=args.binding_id,
        )
    )


def cmd_interaction_task(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_interaction_task(
            args.hermes_instance_id,
            args.title,
            user_id=args.user_id,
            platform_binding_id=args.platform_binding_id,
            conversation_ref=args.conversation_ref,
            description=args.description or "",
            project=args.project,
            priority=args.priority,
            required_capabilities=_csv(args.required_capabilities),
            dependencies=_csv(args.dependencies),
            metadata=_json_arg(args.metadata, {}),
            max_attempts=args.max_attempts,
            actor=args.actor,
        )
    )


def cmd_task_migrate_beads(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path
    from mac.beads_migrator import migrate, read_beads_memories_via_cli

    repo_path = _Path(args.repo_path).expanduser().resolve()
    if args.tickets_only:
        cp = None
        memories = None
    else:
        cp = _plane(args)
        memories = {} if args.no_memories else read_beads_memories_via_cli(repo_path)
    report = migrate(
        repo_path,
        cp,
        project=args.project,
        actor=args.actor,
        dry_run=args.dry_run,
        emit_tickets=not args.no_tickets,
        memories=memories,
        tickets_only=args.tickets_only,
    )
    _print(report.to_dict())


def cmd_task_detect_beads(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path
    from dataclasses import asdict
    from mac.beads_migrator import detect

    _print(asdict(detect(_Path(args.repo_path).expanduser().resolve())))


def cmd_task_detect_ticketing(args: argparse.Namespace) -> None:
    """Connector-aware detection: which ticketing sources a repo has + whether a
    one-way ledger import should be offered."""
    _print(_plane(args).detect_ticketing(args.repo_path))


def cmd_task_convert_ticketing(args: argparse.Namespace) -> None:
    """Run the one-way conversion of a detected foreign source (e.g. beads) into
    MAC ledger tasks plus optional local compatibility files."""
    _print(
        _plane(args).convert_ticketing_source(
            args.repo_path, project=args.project, actor=args.actor, dry_run=args.dry_run
        )
    )


def _default_project_from_cwd() -> Optional[str]:
    """Infer a task's project from the working directory (bd parity).

    `bd` tagged new issues with the repo they were filed from; `mac task
    create` historically left ``project`` null unless ``--project`` was passed,
    so work filed from a checkout never showed up under that project. Use the
    git top-level directory name (works from any subdirectory of a checkout),
    falling back to the cwd basename outside git. Returns None when nothing
    sensible can be derived.
    """
    import subprocess

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        root = top.stdout.strip()
        if top.returncode == 0 and root:
            return os.path.basename(root) or None
    except Exception:
        pass
    try:
        return os.path.basename(os.getcwd()) or None
    except OSError:
        return None


def _effective_read_project(args: argparse.Namespace) -> Optional[str]:
    """Project filter for read commands (list/ready/search), bd parity.

    ``--all`` clears the filter (every project); ``--project NAME`` picks one;
    otherwise default to the working directory's project. Returns None to mean
    "every project".
    """
    if getattr(args, "all", False):
        return None
    proj = getattr(args, "project", None)
    if proj is not None:
        return proj or None
    inferred = _default_project_from_cwd()
    if inferred:
        print(
            "mac: scoping to project %r (use --all for every project, "
            "--project NAME to choose)" % inferred,
            file=sys.stderr,
        )
    return inferred


def _maybe_emit_ticket(
    result: Any, args: argparse.Namespace, *, close_reason: Optional[str] = None
) -> None:
    """Mirror a created/closed task to ``.tickets/<id>.md`` (parity-tickets-autoemit-01).

    Best-effort and opt-out (``--no-ticket`` / ``MAC_NO_TICKET_MIRROR``); never
    fails the command if the mirror can't be written.
    """
    if getattr(args, "no_ticket", False):
        return
    data = result.to_dict() if hasattr(result, "to_dict") else result
    try:
        from mac.tickets_mirror import emit

        path = emit(data, close_reason=close_reason)
    except Exception:  # noqa: BLE001 - the mirror is a convenience, not the operation
        return
    if path:
        print("mac: wrote ticket mirror %s" % path, file=sys.stderr)


def cmd_task_create(args: argparse.Namespace) -> None:
    cp = _plane(args)
    description = _read_text_arg(
        args.description,
        getattr(args, "description_file", None),
        label="--description",
        default="",
    )
    metadata = _read_json_arg(
        args.metadata,
        getattr(args, "metadata_file", None),
        label="--metadata",
        default={},
    )
    if getattr(args, "no_dispatch", False):
        # Stage the task: the loop-mode fleet won't auto-claim it (and it's
        # hidden from `task ready`) until an operator starts it explicitly.
        metadata["no_dispatch"] = True
    if getattr(args, "no_decompose", False):
        # Handoff / plan-note guard: the executor will not auto-decompose this
        # task into child tasks (add_child_tasks refuses with no_decompose).
        metadata["no_decompose"] = True
    # bd parity: when --project is omitted, tag the task with the working
    # directory's project (git repo name, else cwd basename). Pass an explicit
    # --project (including --project '' for none) to override.
    project = args.project
    if project is None:
        project = _default_project_from_cwd()
        if project:
            print(
                "mac: tagging task with project %r (inferred from cwd; "
                "pass --project to override, --project '' for none)" % project,
                file=sys.stderr,
            )
    project = project or None
    created = cp.create_task(
        args.title,
        description=description,
        project=project,
        priority=args.priority,
        required_capabilities=_csv(args.required_capabilities),
        dependencies=_csv(args.dependencies),
        metadata=metadata,
        max_attempts=args.max_attempts,
        actor=args.actor,
    )
    _maybe_emit_ticket(created, args)
    _print(created)


def cmd_task_list(args: argparse.Namespace) -> None:
    cp = _plane(args)
    project = _effective_read_project(args)
    tasks = [task.to_dict() for task in cp.list_tasks(args.state)]
    if project is not None:
        tasks = [t for t in tasks if t.get("project") == project]
    _print(tasks)


def cmd_task_show(args: argparse.Namespace) -> None:
    _print(_plane(args).task_detail(args.task_id))


def cmd_task_ready(args: argparse.Namespace) -> None:
    """List tasks ready to work — state=open, all dependencies completed,
    unclaimed. Works in hub mode (parity-ready-http-01) and local --db mode."""
    cp = _plane(args)
    project = _effective_read_project(args)
    _print([t.to_dict() for t in cp.ready_tasks(project=project, limit=args.limit or None)])


def cmd_task_claim(args: argparse.Namespace) -> None:
    cp = _plane(args)
    task, lease = cp.claim_task(args.task_id, args.agent_id)
    _print({"task": task.to_dict(), "lease_id": lease.id if lease else None})


def cmd_task_close(args: argparse.Namespace) -> None:
    cp = _plane(args)
    from mac.models import TaskState

    detail = {"reason": args.reason} if args.reason else {}
    target = TaskState.COMPLETED.value if args.success else TaskState.CANCELLED.value
    result = cp.transition_task(args.task_id, target, args.actor, detail)
    _maybe_emit_ticket(result, args, close_reason=args.reason or None)
    _print(result)


def cmd_task_reopen(args: argparse.Namespace) -> None:
    # Recovery: return a stuck/terminal task to OPEN (failed/cancelled reset
    # attempt_count so the requeue isn't immediately re-exhausted).
    _print(_plane(args).reopen_task(args.task_id, args.actor, args.reason or None))


def cmd_task_force_complete(args: argparse.Namespace) -> None:
    # Operator override: mark a task COMPLETED regardless of state/review, for
    # reconciling work done out-of-band or recovering a stranded terminal task.
    _print(_plane(args).force_complete_task(args.task_id, args.actor, args.reason or None))


def cmd_task_search(args: argparse.Namespace) -> None:
    cp = _plane(args)
    project = _effective_read_project(args)
    _print([t.to_dict() for t in cp.search_tasks(args.query, project=project, limit=int(args.limit))])


def cmd_task_stats(args: argparse.Namespace) -> None:
    cp = _plane(args)
    project = _effective_read_project(args)
    _print(cp.task_stats(project=project))


def cmd_project_list(args: argparse.Namespace) -> None:
    _print(_plane(args).list_projects())


def cmd_project_create(args: argparse.Namespace) -> None:
    # New projects default to dispatch-PAUSED so a freshly-onboarded backlog
    # does not auto-claim before an operator activates the project. Pass
    # --active to opt straight into autonomous dispatch.
    _print(
        _plane(args).create_project(
            args.name,
            description=args.description or "",
            metadata=_json_arg(args.metadata, {}),
            status=args.status,
            actor=args.actor,
            project_id=args.project_id,
            dispatch_paused=args.dispatch_paused,
        )
    )


def cmd_project_onboard(args: argparse.Namespace) -> None:
    # URL-only onboarding: derive the project name from the repo, clone a
    # task-owned worktree, and create one onboarding task that instructs a
    # worker to read the repo's own README.md / AGENTS.md / PLAN.md (+ manifests)
    # and author the .mac/project.yaml contract. Everything except the URL
    # defaults — this is the "sane-defaults, just give me a repo URL" path.
    capabilities = list(_csv(args.required_capabilities)) or None
    _print(
        _plane(args).onboard_repository(
            args.repository_url,
            project=args.project,
            default_branch=args.default_branch,
            title=args.title,
            priority=args.priority,
            required_capabilities=capabilities,
            actor=args.actor,
        ).to_dict()
    )


def cmd_project_pause(args: argparse.Namespace) -> None:
    _print(_plane(args).set_project_dispatch(args.project, paused=True, actor=args.actor))


def cmd_project_activate(args: argparse.Namespace) -> None:
    _print(_plane(args).set_project_dispatch(args.project, paused=False, actor=args.actor))


def cmd_project_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_project(args.project))


def cmd_task_start(args: argparse.Namespace) -> None:
    _print(_plane(args).start_task(args.task_id, args.agent_id))


def cmd_task_release(args: argparse.Namespace) -> None:
    _print(_plane(args).release_task(args.task_id, actor=args.actor))


def cmd_task_submit(args: argparse.Namespace) -> None:
    _print(_plane(args).submit_for_review(args.task_id, args.agent_id))


def cmd_task_evidence(args: argparse.Namespace) -> None:
    _print(
        _plane(args).add_evidence(
            args.task_id,
            args.kind,
            args.uri,
            args.summary,
            args.created_by,
            checksum=args.checksum,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_machine_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_machine(
            args.hostname,
            labels=_json_arg(args.labels, {}),
            resources=_json_arg(args.resources, {}),
            trusted=not args.untrusted,
            machine_id=args.machine_id,
        )
    )


def cmd_agent_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_agent(
            args.machine_id,
            args.name,
            capabilities=_csv(args.capabilities),
            resources=_json_arg(args.resources, {}),
            agent_id=args.agent_id,
            hermes_instance_id=args.hermes_instance_id,
        )
    )


def cmd_agent_list(args: argparse.Namespace) -> None:
    _print([agent.to_dict() for agent in _plane(args).list_agents()])


def cmd_agent_delete(args: argparse.Namespace) -> None:
    """Hard-delete an agent record (mood/nap/events/messages); task history is
    task-keyed and preserved. Refused while the agent holds an active lease."""
    _plane(args).delete_agent(args.agent_id, actor=args.actor or "human")
    _print({"deleted": args.agent_id})


def cmd_agent_migrate(args: argparse.Namespace) -> None:
    """Move an agent (soul + memory) to a new host. Dry-run by default; pass
    --execute to run the backup -> retarget -> deploy -> restore -> verify
    playbook. The agent NAME is preserved, so its hub-stored persona / memories
    / mood follow ``agent_<name>`` automatically."""
    import shutil
    import time

    import yaml

    from mac import agent_migrate as am
    from mac.hermes_config_surface import registry_path

    reg_path = registry_path()
    registry = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
    fleets = registry.get("fleets") or {}
    fleet = args.fleet or next(
        (f for f, d in fleets.items()
         if any((a or {}).get("name") == args.name for a in (d.get("agents") or []))),
        None,
    )
    if not fleet or fleet not in fleets:
        raise SystemExit("agent %r not found in any fleet in %s" % (args.name, reg_path))
    agents = fleets[fleet].get("agents") or []
    cur = next((a for a in agents if (a or {}).get("name") == args.name), None)
    if cur is None:
        raise SystemExit("agent %r not in fleet %r" % (args.name, fleet))
    src = args.from_target or cur.get("target")
    if not src:
        raise SystemExit("no source target for %r; pass --from" % args.name)

    steps = am.migration_plan(
        args.name,
        src_target=src,
        dst_target=args.to_target,
        fleet=fleet,
        keep_source=args.keep_source,
        retire_source_agent=args.retire_source_agent,
    )
    if not args.execute:
        print(am.render_plan(args.name, steps))
        return
    # --execute: retarget fleets.yaml (backup first), then run the playbook.
    backup = "%s.bak.%d" % (reg_path, int(time.time()))
    shutil.copy2(reg_path, backup)
    am.retarget_fleet_agent(registry, fleet, args.name, target=args.to_target, os=args.to_os)
    reg_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    print("retargeted %s -> %s in %s (backup: %s)" % (args.name, args.to_target, reg_path, backup))
    _print(am.execute_migration(args.name, steps))


def cmd_agent_hardware(args: argparse.Namespace) -> None:
    """Fleet hardware inventory from self-reported resources["hardware"], with the
    hub-derived gen capability (can this agent host media generation, and is it
    currently advertising a gen endpoint)."""
    from mac.hardware import summarize
    from mac.local_gen_catalog import models_for_hardware
    from mac.media_routing import is_gen_capable

    rows = []
    for agent in _plane(args).list_agents():
        data = agent.to_dict() if hasattr(agent, "to_dict") else agent
        resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
        hardware = resources.get("hardware") if isinstance(resources, dict) else None
        serving = bool(isinstance(resources, dict) and resources.get("media_routes"))
        capable = is_gen_capable(hardware) if isinstance(hardware, dict) else False
        # Routable-today (image) catalog models this agent's hardware can run.
        runnable = [m.id for m in models_for_hardware(hardware) if m.routable] if isinstance(hardware, dict) else []
        rows.append({
            "agent": data.get("name") or data.get("id"),
            "accelerator": (hardware or {}).get("accelerator", "unknown") if isinstance(hardware, dict) else "unreported",
            "gen": ("serving" if serving else "capable") if capable else ("serving(cpu)" if serving else "no"),
            "runnable_models": runnable,
            "hardware": summarize(hardware) if isinstance(hardware, dict) else "(no hardware reported — agent predates self-reporting; redeploy to populate)",
        })
    _print(rows)


def cmd_agent_heartbeat(args: argparse.Namespace) -> None:
    _print(
        _plane(args).heartbeat_agent(
            args.agent_id,
            status=args.status,
            health_status=args.health_status,
            resources=_json_arg(args.resources, None),
            running_digest=args.running_digest,
        )
    )


def cmd_fleet_build_distribution(args: argparse.Namespace) -> None:
    _print(_plane(args).fleet_build_distribution())


def _sender_agent_id(args: argparse.Namespace) -> str:
    sender = (
        getattr(args, "sender_agent_id", None)
        or os.environ.get("MAC_AGENT_ID")
        or os.environ.get("MAC_WORKER_AGENT_ID")
    )
    if not sender:
        raise MACError(
            "admin/control sender agent id is required; pass --sender-agent-id or set MAC_AGENT_ID"
        )
    return sender


def cmd_fleet_refresh_source(args: argparse.Namespace) -> None:
    recipients = list(args.agent_id or [])
    _print(
        _plane(args).publish_agentbus_repo_update(
            sender_agent_id=_sender_agent_id(args),
            recipient_agent_ids=recipients,
            all_agents=not recipients,
            repo_path=args.repo_path,
            remote=args.remote,
            branch=args.branch,
            restart=not args.no_restart,
            restart_services=list(args.restart_service or []),
            request_id=args.request_id,
        )
    )


def cmd_fleet_snapshot(args: argparse.Namespace) -> None:
    """fleet-02: the team roster + what each agent is doing now."""
    _print(_plane(args).fleet_snapshot(exclude_agent_id=getattr(args, "agent", None)))


def cmd_openshell_render_policy(args: argparse.Namespace) -> None:
    """Render the OpenShell guardrail policy from the operator template + fleet
    values; install at ~/.mac/openshell-policy.yaml (or print). The policy half
    of executor sandbox enforcement (flipping it on additionally needs the
    Hermes-runtime sandbox image)."""
    from mac import openshell_policy as _op

    template = Path(args.template).expanduser().read_text(encoding="utf-8")
    rendered = _op.render_policy(
        template,
        agent_user=args.agent_user,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        model_gateway_host=getattr(args, "model_gateway_host", None),
        shared_services={"qdrant": args.qdrant_port, "firecrawl": args.firecrawl_port},
        image_runtime=getattr(args, "image_runtime", None),
    )
    if args.into:
        dest = Path(args.into).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        dest.chmod(0o600)
        _print({"wrote": str(dest), "bytes": len(rendered)})
    else:
        sys.stdout.write(rendered)


def cmd_openshell_policy_create(args: argparse.Namespace) -> None:
    policy_text = _read_text_arg(args.policy_text, args.policy_file, label="policy")
    _print(
        _plane(args).create_openshell_policy(
            args.name,
            policy_text,
            description=args.description or "",
            metadata=_read_json_arg(args.metadata, args.metadata_file, label="metadata", default={}),
            created_by=args.created_by,
            policy_id=args.policy_id,
        )
    )


def cmd_openshell_policy_list(args: argparse.Namespace) -> None:
    _print(
        [
            policy.to_dict() if hasattr(policy, "to_dict") else policy
            for policy in _plane(args).list_openshell_policies(
                include_deleted=args.include_deleted
            )
        ]
    )


def cmd_openshell_policy_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_openshell_policy(args.policy, include_deleted=True))


def cmd_openshell_policy_update(args: argparse.Namespace) -> None:
    policy_text = _read_text_arg(args.policy_text, args.policy_file, label="policy", default="")
    metadata = None
    if args.metadata is not None or args.metadata_file is not None:
        metadata = _read_json_arg(args.metadata, args.metadata_file, label="metadata", default={})
    _print(
        _plane(args).update_openshell_policy(
            args.policy,
            name=args.name,
            description=args.description,
            policy_text=policy_text or None,
            metadata=metadata,
            updated_by=args.updated_by,
        )
    )


def cmd_openshell_policy_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_openshell_policy(args.policy, actor=args.actor))


def cmd_openshell_policy_render(args: argparse.Namespace) -> None:
    shared = _read_json_arg(args.shared_services, args.shared_services_file, label="shared_services", default={})
    rendered = _plane(args).render_openshell_policy(
        args.policy,
        agent_user=args.agent_user,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        model_gateway_host=args.model_gateway_host,
        shared_services=shared,
    )
    if args.into:
        dest = Path(args.into).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered["policy_text"], encoding="utf-8")
        dest.chmod(0o600)
        _print({**rendered, "wrote": str(dest), "policy_text": ""})
    else:
        _print(rendered)


def cmd_openshell_policy_assign(args: argparse.Namespace) -> None:
    _print(
        _plane(args).assign_openshell_policy(
            args.policy,
            target_type=args.target_type,
            target_id=args.target_id,
            created_by=args.created_by,
        )
    )


def cmd_openshell_policy_versions(args: argparse.Namespace) -> None:
    _print(
        [
            version.to_dict() if hasattr(version, "to_dict") else version
            for version in _plane(args).list_openshell_policy_versions(args.policy)
        ]
    )


def cmd_openshell_status(args: argparse.Namespace) -> None:
    _print(_plane(args).get_openshell_status(args.agent))


def cmd_openshell_policy_deploy_status(args: argparse.Namespace) -> None:
    _print(_plane(args).get_openshell_status(args.agent))


def cmd_openshell_reconcile(args: argparse.Namespace) -> None:
    from mac.openshell_reconcile import (
        default_fleets_path,
        fleet_agent_names,
        load_fleet_config,
        reconcile_openshell_agents,
    )

    policy_text = None
    if args.policy_file:
        policy_text = Path(args.policy_file).expanduser().read_text(encoding="utf-8")
    detail = _read_json_arg(args.detail, args.detail_file, label="detail", default={})
    selected_agents = list(args.agent or [])
    explicit_agents = bool(selected_agents)
    if not selected_agents:
        cfg_path = Path(args.fleet_config).expanduser() if args.fleet_config else default_fleets_path()
        cfg = load_fleet_config(cfg_path)
        selected_agents = fleet_agent_names(cfg, args.target_fleet or args.fleet)
    if args.apply and not args.no_report_status and args.status == "active" and not args.validated:
        raise SystemExit("--validated is required with --apply when reporting status=active")
    _print(
        reconcile_openshell_agents(
            _plane(args),
            agent_selectors=selected_agents,
            policy_name=args.policy_name,
            policy_text=policy_text,
            apply=args.apply,
            actor=args.actor,
            status=args.status,
            validated=args.validated,
            sandbox_id=args.sandbox_id,
            detail=detail,
            runtime=args.runtime,
            openshell_version=args.openshell_version,
            gateway_driver=args.gateway_driver,
            image=args.image,
            validation_summary=args.validation_summary or "",
            report_status=not args.no_report_status,
            allow_missing_agents=(not explicit_agents and not args.strict),
        )
    )


def _soul_snapshot_setup(args):
    """Resolve (fleet_name, agents, transport) for the soul pull/push commands."""
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    cfg = _yaml.safe_load(Path(args.fleets_config).expanduser().read_text(encoding="utf-8")) or {}
    fleet_name = args.fleet or next(iter((cfg.get("fleets") or {})), None)
    if not fleet_name:
        raise SystemExit("no fleet found; pass --fleet")
    agents = _ss.load_fleet_agents(cfg, fleet_name)
    return fleet_name, agents, _ss.SSHTransport()


def cmd_fleet_soul_pull(args: argparse.Namespace) -> None:
    """Phase 1 fleet snapshot: pull each agent's editable soul text into a local tree."""
    from datetime import datetime, timezone
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    fleet_name, agents, transport = _soul_snapshot_setup(args)
    dest = Path(args.into).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = _ss.pull_snapshot(
        agents, dest, transport, fleet=fleet_name, pulled_at=stamp,
        memory_checksum=getattr(args, "memory_checksum", False),
    )
    # Phase 3: also capture hub-stored persona + mood (resolve agent ids by name).
    if getattr(args, "with_hub", False):
        hub = _plane(args)
        by_name = {}
        try:
            for a in hub.list_agents():
                ad = a.to_dict() if hasattr(a, "to_dict") else dict(a)
                if ad.get("name"):
                    by_name[ad["name"]] = ad.get("id")
        except Exception as exc:  # noqa: BLE001
            print("mac: hub agent list failed (%s); skipping persona/mood" % exc, file=sys.stderr)
        ids = [(n, by_name.get(n) or "agent_%s" % n) for n, _t in agents]
        hub_section = _ss.capture_hub_state(hub, ids, dest, pulled_at=stamp)
        manifest["hub"] = hub_section
    (dest / "manifest.yaml").write_text(_yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    summary = {
        "fleet": fleet_name, "into": str(dest), "pulled_at": stamp,
        "agents": {
            n: {
                "soul": {f: m.get("present") for f, m in a["files"].items()},
                "memory_refs": {f: m.get("bytes") for f, m in a.get("memory", {}).items()
                                if m.get("present")},
            }
            for n, a in manifest["agents"].items()
        },
    }
    if "hub" in manifest:
        summary["hub"] = {n: {"persona": s["persona"].get("present"), "mood": s["mood"].get("present")}
                          for n, s in manifest["hub"]["agents"].items()}
    _print(summary)


def cmd_fleet_soul_push(args: argparse.Namespace) -> None:
    """Phase 1 fleet snapshot: diff edited soul tree vs live, back up + write changes."""
    from datetime import datetime, timezone
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    src = Path(getattr(args, "from_dir")).expanduser()
    manifest = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    transport = _ss.SSHTransport()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    res = _ss.plan_and_push(
        src, manifest, transport, stamp=stamp,
        dry_run=args.dry_run, only_agents=(args.agent or None),
    )
    _print({
        "dry_run": res.dry_run,
        "changes": [
            {"agent": c.agent, "file": c.relpath, "status": c.status,
             "applied": c.applied, "backup": c.backup_path}
            for c in res.changes
        ],
        "to_apply": [f"{c.agent}/{c.relpath}" for c in res.to_apply],
    })


def cmd_fleet_memory_export(args: argparse.Namespace) -> None:
    """Phase 2b: export the fleet's Qdrant vector memory to greppable JSONL for
    vetting (find stale facts that wouldn't surface from the soul text)."""
    import json as _json
    from mac import memory_vetting as _mv

    client = _mv.QdrantClient(args.qdrant_url)
    collections = _csv(args.collections) if args.collections else list(_mv.DEFAULT_COLLECTIONS)
    records = _mv.export_memory_records(client.scroll, collections, agent_id=args.agent or None)
    if args.search:
        records = _mv.search_records(records, args.search)
    if args.into:
        dest = Path(args.into).expanduser()
        dest.write_text("\n".join(_json.dumps(r, default=str) for r in records) + "\n", encoding="utf-8")
        _print({"qdrant": args.qdrant_url, "collections": collections, "records": len(records),
                "into": str(dest), "search": args.search})
    else:
        for r in records:
            sys.stdout.write(_json.dumps(r, default=str) + "\n")


def cmd_fleet_memory_prune(args: argparse.Namespace) -> None:
    """Phase 2b: delete vetted Qdrant point ids from a collection (destructive;
    operator-vetted). Ids come from --id (repeatable) or a JSONL export via
    --from-jsonl (uses each record's id)."""
    import json as _json
    from mac import memory_vetting as _mv

    ids: List[Any] = list(args.id or [])
    if args.from_jsonl:
        for line in Path(args.from_jsonl).expanduser().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = _json.loads(line)
                if rec.get("id") is not None:
                    ids.append(rec["id"])
    client = _mv.QdrantClient(args.qdrant_url)
    _print(_mv.prune_points(client.delete, args.collection, ids))


def _hub_get_mood(agent_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """GET the agent's current mood overlay straight from the hub HTTP API.

    The fleet-context refresh otherwise runs against local SQLite ($MAC_DB wins
    in CLI resolution), so on a spoke a hub-set mood would never be seen. This
    talks to the hub directly (same URL + token the worker uses). Returns the
    overlay dict, or None on any error / no active mood."""
    import json as _json
    import os as _os
    import urllib.request as _u

    base = (_os.environ.get("MAC_HUB_URL") or _os.environ.get("MAC_URL") or "").rstrip("/")
    token = (_os.environ.get("MAC_WORKER_TOKEN") or _os.environ.get("MAC_API_TOKEN") or "").strip()
    if not (base and token and agent_id):
        return None
    req = _u.Request("%s/agents/%s/mood" % (base, agent_id), method="GET")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/json")
    try:
        with _u.urlopen(req, timeout=10) as resp:  # noqa: S310 (operator-configured hub)
            raw = resp.read().decode("utf-8", "replace")
        data = _json.loads(raw) if raw.strip() else None
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - mood is best-effort
        return None


def cmd_fleet_refresh_context(args: argparse.Namespace) -> None:
    """fleet-02 + mood-01: refresh the live Fleet section AND this agent's mood
    overlay in its runtime-context markdown, so its next session knows what
    teammates are doing and actually behaves in its current mood. Idempotent."""
    import os as _os
    from pathlib import Path as _Path

    from mac.hermes_runtime import (
        refresh_fleet_section,
        refresh_mood_section,
        render_fleet_section,
        render_mood_section,
    )

    plane = _plane(args)
    agent = getattr(args, "agent", None)
    snapshot = plane.fleet_snapshot(exclude_agent_id=agent)
    markdown = getattr(args, "markdown", None) or _os.environ.get(
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"
    ) or str(_Path.home() / ".hermes" / "mac-runtime-context.md")
    path = _Path(markdown)
    refresh_fleet_section(path, render_fleet_section(snapshot))

    # mood-01: fetch this agent's mood straight from the hub HTTP API so a spoke
    # picks it up even though $MAC_DB pins the rest of the CLI to local SQLite
    # (fleet_snapshot above isn't wrapped in hub mode, so it stays local). Best-effort.
    overlay = _hub_get_mood(agent) if agent else None
    refresh_mood_section(path, render_mood_section(overlay))
    _print(
        {
            "status": "refreshed",
            "markdown": markdown,
            "members": len(snapshot.get("members", [])),
            "mood": (overlay or {}).get("mode"),
        }
    )


def cmd_journal_snapshot(args: argparse.Namespace) -> None:
    """journal-01: snapshot this agent's soul + memory state into
    $HOME/.mac/journal/<date>/ and run the backup hook (unless --no-hook)."""
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    home = _Path(args.home).expanduser() if getattr(args, "home", None) else None
    m = _journal.snapshot(
        home=home,
        root=root,
        date=getattr(args, "date", None),
        agent_id=getattr(args, "agent", None),
        run_hook=not getattr(args, "no_hook", False),
    )
    _print(
        {
            "date": m["date"],
            "agent_id": m["agent_id"],
            "captured": m["captured"],
            "files": len(m["files"]),
            "path": str((root or _journal.journal_root()) / m["date"]),
            "hook": m.get("hook"),
        }
    )


def cmd_journal_list(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    _print(_journal.list_journals(root))


def cmd_journal_restore(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    home = _Path(args.home).expanduser() if getattr(args, "home", None) else None
    _print(
        _journal.restore(
            args.date, home=home, root=root, dry_run=getattr(args, "dry_run", False)
        )
    )


def _fleet_setup_plan_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    from mac.fleet_setup import build_setup_plan, load_setup_spec, public_plan

    root = Path(__file__).resolve().parents[2]
    fleets_config = Path(args.fleets_config).expanduser()
    env_file = Path(args.env_file).expanduser()
    spec = load_setup_spec(Path(args.spec).expanduser())
    return public_plan(
        build_setup_plan(
            spec,
            root=root,
            fleets_config=fleets_config,
            env_file=env_file,
        )
    )


def cmd_fleet_validate_setup(args: argparse.Namespace) -> None:
    """Validate a declarative mac.fleet_setup.v1 spec."""
    _print(_fleet_setup_plan_from_args(args))


def cmd_fleet_doctor_setup(args: argparse.Namespace) -> None:
    """Run LLM-friendly setup doctor checks for a declarative fleet spec."""
    plan = _fleet_setup_plan_from_args(args)
    _print(
        {
            "schema": "mac.fleet_setup_doctor.v1",
            "status": plan.get("status"),
            "hub": plan.get("hub"),
            "fleet_name": plan.get("fleet_name"),
            "checks": plan.get("checks"),
            "required_env": plan.get("required_env"),
            "warnings": plan.get("warnings"),
            "errors": plan.get("errors"),
            "next_steps": plan.get("next_steps"),
        }
    )


def cmd_mood_set(args: argparse.Namespace) -> None:
    _print(
        _plane(args).set_mood(
            args.agent_id,
            args.mode,
            set_by=args.set_by,
            reason=args.reason,
            ttl_seconds=args.ttl_seconds,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_mood_show(args: argparse.Namespace) -> None:
    overlay = _plane(args).get_current_mood(args.agent_id)
    _print(overlay.to_dict() if overlay is not None else None)


def cmd_mood_clear(args: argparse.Namespace) -> None:
    cleared = _plane(args).clear_mood(
        args.agent_id, cleared_by=args.cleared_by, reason=args.reason
    )
    _print(cleared.to_dict() if cleared is not None else None)


def cmd_mood_history(args: argparse.Namespace) -> None:
    _print(
        [
            overlay.to_dict()
            for overlay in _plane(args).list_mood_history(args.agent_id, limit=args.limit)
        ]
    )


def cmd_nap_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_nap(
            args.agent_id,
            offset_minutes=args.offset_minutes,
            window_minutes=args.window_minutes,
            enabled=not args.disabled,
            actor=args.actor,
        )
    )


def cmd_nap_show(args: argparse.Namespace) -> None:
    schedule = _plane(args).get_nap_schedule(args.agent_id)
    _print(schedule.to_dict() if schedule is not None else None)


def cmd_nap_next(args: argparse.Namespace) -> None:
    _print(_plane(args).next_nap_window(args.agent_id))


def cmd_nap_begin(args: argparse.Namespace) -> None:
    _print(
        _plane(args).begin_nap(
            args.agent_id,
            actor=args.actor,
            detail=_json_arg(args.detail, {}),
        )
    )


def cmd_nap_complete(args: argparse.Namespace) -> None:
    _print(
        _plane(args).complete_nap(
            args.run_id,
            summary_evidence_id=args.evidence_id,
            detail=_json_arg(args.detail, None),
            actor=args.actor,
        )
    )


def cmd_nap_fail(args: argparse.Namespace) -> None:
    _print(_plane(args).fail_nap(args.run_id, args.reason, actor=args.actor))


def cmd_nap_list(args: argparse.Namespace) -> None:
    _print([run.to_dict() for run in _plane(args).list_nap_runs(args.agent_id)])


def cmd_dispatch_once(args: argparse.Namespace) -> None:
    _print(_plane(args).dispatch_once(args.lease_seconds))


def cmd_dispatch_tick(args: argparse.Namespace) -> None:
    _print(_plane(args).tick(args.lease_seconds, args.limit))


def cmd_message_send(args: argparse.Namespace) -> None:
    _print(
        _plane(args).send_message(
            args.sender_agent_id,
            args.recipient_agent_id,
            args.message_type,
            _json_arg(args.payload, {}),
            task_id=args.task_id,
        )
    )


def cmd_message_inbox(args: argparse.Namespace) -> None:
    _print([message.to_dict() for message in _plane(args).deliver_messages(args.agent_id, args.limit)])


def _agentbus_payload_arg(args: argparse.Namespace) -> Any:
    if args.payload is None:
        return None
    if args.payload_encoding == "json":
        return json.loads(args.payload)
    return args.payload


def cmd_agentbus_open(args: argparse.Namespace) -> None:
    _print(
        _plane(args).open_agentbus_stream(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_id=args.recipient_agent_id,
            content_type=args.content_type,
            topic=args.topic,
            headers=_json_arg(args.headers, {}),
            task_id=args.task_id,
            stream_id=args.stream_id,
        )
    )


def cmd_agentbus_append(args: argparse.Namespace) -> None:
    _print(
        _plane(args).append_agentbus_chunk(
            args.stream_id,
            sender_agent_id=args.sender_agent_id,
            payload=_agentbus_payload_arg(args),
            content_type=args.content_type,
            payload_encoding=args.payload_encoding,
            final=args.final,
        )
    )


def cmd_agentbus_close(args: argparse.Namespace) -> None:
    _print(
        _plane(args).close_agentbus_stream(
            args.stream_id,
            sender_agent_id=args.sender_agent_id,
            status=args.status,
        )
    )


def cmd_agentbus_list(args: argparse.Namespace) -> None:
    _print(
        [
            stream.to_dict()
            for stream in _plane(args).list_agentbus_streams(
                agent_id=args.agent_id,
                status=args.status,
                limit=args.limit,
            )
        ]
    )


def cmd_agentbus_read(args: argparse.Namespace) -> None:
    _print(
        [
            chunk.to_dict()
            for chunk in _plane(args).read_agentbus_chunks(
                args.agent_id,
                args.stream_id,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        ]
    )


def cmd_agentbus_publish(args: argparse.Namespace) -> None:
    _print(
        _plane(args).publish_agentbus_content(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_id=args.recipient_agent_id,
            content_type=args.content_type,
            payload=_agentbus_payload_arg(args),
            topic=args.topic,
            headers=_json_arg(args.headers, {}),
            task_id=args.task_id,
            payload_encoding=args.payload_encoding,
        )
    )


def cmd_agentbus_repo_update(args: argparse.Namespace) -> None:
    if not args.recipient_agent_id and not args.all_agents:
        raise MACError("repo-update requires --recipient-agent-id or --all-agents")
    _print(
        _plane(args).publish_agentbus_repo_update(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_ids=args.recipient_agent_id,
            all_agents=args.all_agents,
            repo_path=args.repo_path,
            remote=args.remote,
            branch=args.branch,
            restart=not args.no_restart,
            restart_services=list(args.restart_service or []),
            request_id=args.request_id,
        )
    )


def cmd_agentbus_artifact_publish(args: argparse.Namespace) -> None:
    _print(
        _plane(args).publish_agentbus_artifact(
            sender_agent_id=args.sender_agent_id,
            operation=args.operation,
            recipient_agent_ids=list(args.recipient_agent_id or []),
            all_agents=args.all_agents,
            artifact_id=args.artifact,
            digest=args.digest,
            kind=args.kind,
            uri=args.uri,
            public_url=args.public_url,
            path=args.path,
            publish_dir=args.publish_dir,
            sbom_uri=args.sbom_uri,
            signers=list(_csv(args.signers)),
            metadata=_json_arg(args.metadata, {}),
            task_id=args.task_id,
            request_id=args.request_id,
        )
    )


def cmd_review_request(args: argparse.Namespace) -> None:
    _print(_plane(args).request_review(args.task_id, args.reviewer_agent_id, args.actor))


def cmd_review_decision(args: argparse.Namespace) -> None:
    _print(
        _plane(args).submit_review(
            args.review_id,
            args.status,
            args.reviewer_agent_id,
            reason=args.reason,
            evidence_id=args.evidence_id,
        )
    )


def cmd_publish(args: argparse.Namespace) -> None:
    _print(_plane(args).publish_task(args.task_id, args.target, args.created_by, evidence_id=args.evidence_id))


def cmd_pull_request_open(args: argparse.Namespace) -> None:
    from mac.gitops import open_pull_request

    repo_url = args.repo_url
    if not repo_url:
        raise SystemExit("--repo-url is required (or pipe the URL via $MAC_TASK_REPO_URL)")
    result = open_pull_request(
        repo_url,
        args.head,
        base=args.base,
        title=args.title,
        body=args.body,
    )
    output: Dict[str, Any] = {
        "host": result.host,
        "number": result.number,
        "url": result.url,
        "state": result.state,
        "head": args.head,
        "base": args.base or "(default)",
    }
    if args.task_id:
        try:
            finding = _plane(args).record_integration_finding(
                source_kind=result.host,
                source_id="%s#%d" % (result.url, result.number),
                finding_type="pull_request_opened",
                title="PR #%d opened for task %s" % (result.number, args.task_id),
                detail={
                    "task_id": args.task_id,
                    "pull_request_url": result.url,
                    "pull_request_number": result.number,
                    "head": args.head,
                    "base": args.base or "",
                    "repo_url": repo_url,
                },
                severity="info",
            )
            output["finding_id"] = finding.id if hasattr(finding, "id") else None
        except Exception as exc:  # noqa: BLE001
            output["finding_error"] = str(exc)
    _print(output)


def cmd_secret_set(args: argparse.Namespace) -> None:
    value = _resolve_secret_value(args)
    _print(_plane(args).create_secret(args.name, value, _json_arg(args.scopes, {}), args.created_by))


def _resolve_secret_value(args: argparse.Namespace) -> str:
    sources = [bool(args.value), bool(args.from_stdin), bool(args.from_file)]
    if sum(sources) != 1:
        raise MACError("exactly one of <value>, --from-stdin, --from-file is required")
    if args.from_stdin:
        return sys.stdin.read().rstrip("\n")
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as handle:
            return handle.read().rstrip("\n")
    return args.value


def cmd_secret_list(args: argparse.Namespace) -> None:
    _print([secret.to_dict() for secret in _plane(args).list_secrets()])


def cmd_secret_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_secret(args.secret, actor=args.actor))


def cmd_secret_rotate(args: argparse.Namespace) -> None:
    value = _resolve_secret_value(args)
    _print(_plane(args).rotate_secret(args.name, value, actor=args.actor))


def cmd_secret_access(args: argparse.Namespace) -> None:
    _print(_plane(args).request_secret(args.secret, args.agent_id, args.purpose))


def cmd_secret_audits(args: argparse.Namespace) -> None:
    _print([audit.to_dict() for audit in _plane(args).list_secret_audits(args.secret_id)])


def cmd_runtime_create(args: argparse.Namespace) -> None:
    _print(_plane(args).create_runtime(args.name, _json_arg(args.manifest, {}), args.created_by))


def cmd_runtime_list(args: argparse.Namespace) -> None:
    _print([runtime.to_dict() for runtime in _plane(args).list_runtimes()])


def cmd_runtime_delta_propose(args: argparse.Namespace) -> None:
    _print(
        _plane(args).propose_runtime_delta(
            args.task_id,
            args.agent_id,
            args.package_manager,
            _json_arg(args.commands, []),
            _json_arg(args.dependencies, []),
            args.reason,
            project=args.project,
            base_runtime_id=args.base_runtime,
            base_runtime_digest=args.base_runtime_digest,
            lockfile_path=args.lockfile_path,
            lockfile_digest=args.lockfile_digest,
            evidence_id=args.evidence_id,
        )
    )


def cmd_runtime_delta_list(args: argparse.Namespace) -> None:
    _print(
        [
            delta.to_dict()
            for delta in _plane(args).list_runtime_deltas(
                status=args.status,
                task_id=args.task_id,
                project=args.project,
                limit=args.limit,
            )
        ]
    )


def cmd_runtime_delta_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_runtime_delta(args.delta).to_dict())


def cmd_runtime_delta_validate(args: argparse.Namespace) -> None:
    _print(_plane(args).validate_runtime_delta(args.delta, args.actor).to_dict())


def cmd_runtime_delta_reject(args: argparse.Namespace) -> None:
    _print(_plane(args).reject_runtime_delta(args.delta, args.actor, args.reason).to_dict())


def cmd_runtime_delta_promote(args: argparse.Namespace) -> None:
    _print(
        _plane(args).promote_runtime_delta(
            args.delta,
            args.actor,
            runtime_name=args.runtime_name,
        ).to_dict()
    )


def cmd_artifact_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_artifact(
            kind=args.kind,
            digest=args.digest,
            uri=args.uri,
            created_by=args.created_by,
            sbom_uri=args.sbom_uri,
            signers=_csv(args.signers),
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_artifact_list(args: argparse.Namespace) -> None:
    _print([a.to_dict() for a in _plane(args).list_artifacts(args.kind)])


def cmd_artifact_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_artifact(args.artifact))


def cmd_artifact_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_artifact(args.artifact, actor=args.actor))


def cmd_migrate_import(args: argparse.Namespace) -> None:
    report = import_jsonl(_plane(args), path=Path(args.path))
    _print(report.to_dict())


def cmd_migrate_acc(args: argparse.Namespace) -> None:
    report = migrate_acc_sqlite(
        _plane(args),
        Path(args.acc_db),
        mode=args.mode,
        allow_active=args.allow_active,
        audit_limit=args.audit_limit,
        agent_home=Path(args.agent_home) if args.agent_home else None,
    ).to_dict()
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _print(report)


def cmd_env_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_environment(
            args.name,
            tenant_id=args.tenant_id,
            channel=args.channel,
            promotes_from=args.promotes_from,
            metadata=_json_arg(args.metadata, {}),
            created_by=args.created_by,
        )
    )


def cmd_env_list(args: argparse.Namespace) -> None:
    _print([e.to_dict() for e in _plane(args).list_environments(args.tenant_id, args.channel)])


def cmd_env_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_environment(args.environment))


def cmd_env_deploy(args: argparse.Namespace) -> None:
    _print(
        _plane(args).deploy_artifact(
            args.environment,
            args.artifact,
            args.actor,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_env_current(args: argparse.Namespace) -> None:
    current = _plane(args).current_deployment(args.environment)
    _print(current.to_dict() if current is not None else None)


def cmd_env_deployments(args: argparse.Namespace) -> None:
    _print([d.to_dict() for d in _plane(args).list_deployments(args.environment)])


def cmd_bridge_import(args: argparse.Namespace) -> None:
    _print(
        _plane(args).import_project_item(
            args.source,
            args.external_id,
            args.title,
            _json_arg(args.payload, {}),
            required_capabilities=_csv(args.required_capabilities),
            description=args.description,
            project=args.project,
            priority=args.priority,
            dependencies=_csv(args.dependencies),
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def cmd_bridge_list(args: argparse.Namespace) -> None:
    _print([item.to_dict() for item in _plane(args).list_project_items()])


def cmd_bridge_repository_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_project_repository(
            args.name,
            args.path,
            source=args.source,
            project=args.project,
            required_capabilities=_csv(args.required_capabilities),
            enabled=not args.disabled,
            poll_interval_seconds=args.poll_interval_seconds,
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def cmd_bridge_repository_list(args: argparse.Namespace) -> None:
    _print(
        [
            repo.to_dict()
            for repo in _plane(args).list_project_repositories(enabled=args.enabled)
        ]
    )


def cmd_integrations_findings(args: argparse.Namespace) -> None:
    _print(
        [
            finding.to_dict()
            for finding in _plane(args).list_integration_findings(
                source_kind=args.source_kind,
                source_id=args.source_id,
                finding_type=args.finding_type,
                status=args.status,
                severity=args.severity,
                limit=args.limit,
            )
        ]
    )


def cmd_integrations_observations(args: argparse.Namespace) -> None:
    _print(
        [
            observation.to_dict()
            for observation in _plane(args).list_integration_observations(
                source_kind=args.source_kind,
                source_id=args.source_id,
                authority=args.authority,
                status=args.status,
                limit=args.limit,
            )
        ]
    )


def cmd_memory_add(args: argparse.Namespace) -> None:
    _print(
        _plane(args).add_memory(
            task_id=args.task_id,
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            record_type=args.record_type,
            content=args.content,
            evidence_id=args.evidence_id,
            created_by=args.created_by,
        )
    )


def cmd_memory_search(args: argparse.Namespace) -> None:
    _print(
        [
            record.to_dict()
            for record in _plane(args).search_memory(
                task_id=args.task_id,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                record_type=getattr(args, "record_type", None),
                record_type_prefix=getattr(args, "record_type_prefix", None),
                created_by=getattr(args, "created_by", None),
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
                limit=getattr(args, "limit", None),
                order=getattr(args, "order", "asc"),
            )
        ]
    )


def cmd_memory_remember(args: argparse.Namespace) -> None:
    """`bd remember` equivalent — store an ambient project-scoped fact
    keyed by name. Subsequent calls with the same key overwrite."""
    _print(
        _plane(args).remember_memory(
            args.key,
            args.content,
            project=args.project,
            actor=args.actor,
        )
    )


def _build_vector_writer(args: argparse.Namespace):
    """Construct a VectorWriterService for CLI commands. The Qdrant
    endpoint defaults to the same Qdrant env cascade the hub uses, then
    http://127.0.0.1:6333. Tests inject the writer directly; this
    builder is for operator use.
    """
    import os

    from mac.vector_writer_service import VectorWriterService

    cp = _plane(args)
    qdrant_url = (
        getattr(args, "qdrant_url", None)
        or os.environ.get("MAC_QDRANT_URL")
        or os.environ.get("QDRANT_URL")
        or os.environ.get("QDRANT_ADDRESS")
        or os.environ.get("QDRANT_FLEET_URL")
        or "http://127.0.0.1:6333"
    )
    return VectorWriterService(memory=cp.memory, qdrant_url=qdrant_url)


def cmd_memory_embed(args: argparse.Namespace) -> None:
    """mem-07: embed one memory_record into the medium tier."""
    writer = _build_vector_writer(args)
    ref = writer.embed_memory(args.memory_id, tier=args.tier)
    _print(ref.to_dict())


def cmd_memory_backfill(args: argparse.Namespace) -> None:
    """mem-07: embed every memory_record that isn't already in the tier."""
    writer = _build_vector_writer(args)
    _print(writer.backfill(tier=args.tier, limit=args.limit))


def cmd_memory_health(args: argparse.Namespace) -> None:
    """mem-10: memory-tier health snapshot."""
    cp = _plane(args)
    kwargs: Dict[str, Any] = {"nap_interval_hours": args.nap_interval_hours}
    qdrant_url = getattr(args, "qdrant_url", None)
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.memory_health(**kwargs))


def cmd_memory_recall(args: argparse.Namespace) -> None:
    """mem-09: vector-tier recall, hub-routable when MAC_API_URL is set."""
    cp = _plane(args)
    # If the dispatch is local (operator running `mac --db ...`), we
    # need to provide a Qdrant URL; if it's remote, the HTTP route
    # already resolves Qdrant on the hub side.
    qdrant_url = getattr(args, "qdrant_url", None)
    kwargs = {
        "tier": args.tier,
        "limit": args.limit,
        "min_score": args.min_score,
        "project": args.project,
        "tenant_id": args.tenant_id,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.recall_memory(args.query, **kwargs))


def cmd_memory_recall_dreams(args: argparse.Namespace) -> None:
    """Recall typed dream artifacts using scope/kind/confidence filters."""
    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    kwargs = {
        "tier": args.tier,
        "limit": args.limit,
        "min_score": args.min_score,
        "project": args.project,
        "agent_id": args.agent_id,
        "scope": args.scope,
        "kind": args.kind,
        "min_confidence": args.min_confidence,
        "tenant_id": args.tenant_id,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.recall_dream_artifacts(args.query, **kwargs))


def cmd_nap_cycle(args: argparse.Namespace) -> None:
    """mem-08 autonomy: begin + consolidate + complete in one shot."""
    from mac.dispatch import RemoteDispatch

    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    writer = None
    if not args.no_embed and not isinstance(cp, RemoteDispatch):
        writer = _build_vector_writer(args)
    kwargs = {
        "actor": args.actor,
        "vector_writer": writer,
        "embed_into_medium": not args.no_embed,
        "emit_dream_artifacts": not args.no_dreams,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.run_nap_cycle(args.agent_id, **kwargs))


def cmd_nap_due(args: argparse.Namespace) -> None:
    """List agents whose nap window has opened and hasn't been completed."""
    due = _plane(args).list_due_nap_agents(as_of=args.as_of)
    if getattr(args, "format", "json") == "agent-ids":
        # Newline-delimited agent_ids for `xargs` in the nap-tick unit —
        # avoids piping JSON through an embedded interpreter.
        for item in due:
            print(item["agent_id"])
        return
    _print(due)


def cmd_nap_consolidate(args: argparse.Namespace) -> None:
    """mem-08: walk the agent's recent memory_records, write per-group
    summaries, and embed them into the medium tier."""
    from mac.dispatch import RemoteDispatch

    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    writer = None
    if not args.no_embed and not isinstance(cp, RemoteDispatch):
        writer = _build_vector_writer(args)
    kwargs = {
        "since": args.since,
        "nap_run_id": args.nap_run_id,
        "embed_into_medium": not args.no_embed,
        "emit_dream_artifacts": not args.no_dreams,
        "vector_writer": writer,
        "created_by": args.created_by,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.consolidate_nap(args.agent_id, **kwargs))


def cmd_memory_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in _plane(args).list_remembered_memory(project=args.project)
        ]
    )


def cmd_memory_forget(args: argparse.Namespace) -> None:
    _print(_plane(args).forget_memory(args.key, project=args.project))


def cmd_memory_decay(args: argparse.Namespace) -> None:
    """dream-04: forget stale, low-salience memory (dry-run unless --apply)."""
    _print(
        _plane(args).decay_memory(
            ttl_days=args.ttl_days,
            dry_run=not args.apply,
            limit=args.limit,
        )
    )


def cmd_rollout_create(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_rollout(
            args.version,
            args.strategy,
            args.target_percent,
            args.created_by,
            tenant_id=args.tenant_id,
            channel=args.channel,
            runtime_environment_id=args.runtime,
            artifact_uri=args.artifact_uri,
            artifact_hash=args.artifact_hash,
            health_policy=_json_arg(args.health_policy, {}),
            required_eval_set_id=args.required_eval_set_id,
        )
    )


def cmd_eval_set_create(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_eval_set(
            args.name,
            scoring=args.scoring,
            description=args.description or "",
            baseline_score=args.baseline_score,
            regression_threshold=args.regression_threshold,
            metadata=_json_arg(args.metadata, {}),
            created_by=args.created_by,
        )
    )


def cmd_eval_set_list(args: argparse.Namespace) -> None:
    _print([eval_set.to_dict() for eval_set in _plane(args).list_eval_sets()])


def cmd_eval_set_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_eval_set(args.eval_set))


def cmd_eval_set_baseline(args: argparse.Namespace) -> None:
    _print(_plane(args).update_eval_set_baseline(args.eval_set, args.baseline_score, args.actor))


def cmd_eval_run_record(args: argparse.Namespace) -> None:
    _print(
        _plane(args).record_eval_run(
            args.eval_set,
            args.target_kind,
            args.target_id,
            args.score,
            detail=_json_arg(args.detail, {}),
            evidence_id=args.evidence_id,
            created_by=args.created_by,
        )
    )


def cmd_eval_run_list(args: argparse.Namespace) -> None:
    _print([run.to_dict() for run in _plane(args).list_eval_runs(args.eval_set, args.target_id)])


def cmd_events_list(args: argparse.Namespace) -> None:
    _print(
        _plane(args).list_events(
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            actor=args.actor,
            event_type=args.event_type,
            event_type_prefix=args.prefix,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )


def cmd_action_events_list(args: argparse.Namespace) -> None:
    _print(
        [
            event.to_dict() if hasattr(event, "to_dict") else event
            for event in _plane(args).list_action_events(
                agent_id=args.agent_id,
                task_id=args.task_id,
                session_id=args.session_id,
                sandbox_id=args.sandbox_id,
                policy_id=args.policy_id,
                action_type=args.action_type,
                outcome=args.outcome,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
        ]
    )


def cmd_action_events_stream(args: argparse.Namespace) -> None:
    import time as _time

    cursor = args.since
    deadline = None if args.follow else _time.monotonic() + max(0.0, float(args.timeout))
    while True:
        events = list(
            _plane(args).list_action_events(
                agent_id=args.agent_id,
                task_id=args.task_id,
                session_id=args.session_id,
                sandbox_id=args.sandbox_id,
                policy_id=args.policy_id,
                action_type=args.action_type,
                outcome=args.outcome,
                since=cursor,
                limit=args.limit,
            )
        )
        if events:
            for event in reversed(events):
                payload = event.to_dict() if hasattr(event, "to_dict") else event
                print(json.dumps(payload, sort_keys=True))
                cursor = payload.get("timestamp")
            sys.stdout.flush()
        if not args.follow and (deadline is None or _time.monotonic() >= deadline):
            break
        _time.sleep(max(0.25, float(args.interval)))


def cmd_action_events_export_otlp(args: argparse.Namespace) -> None:
    _print(
        _plane(args).export_action_events_otlp(
            agent_id=args.agent_id,
            task_id=args.task_id,
            session_id=args.session_id,
            sandbox_id=args.sandbox_id,
            policy_id=args.policy_id,
            action_type=args.action_type,
            outcome=args.outcome,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )


def cmd_command_audit_list(args: argparse.Namespace) -> None:
    _print(
        [
            record.to_dict()
            for record in _plane(args).list_command_audit(
                agent_id=args.agent_id,
                task_id=args.task_id,
                command_id=args.command_id,
                phase=args.phase,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
        ]
    )


def cmd_observability_list(args: argparse.Namespace) -> None:
    _print(
        [
            event.to_dict()
            for event in _plane(args).list_observability(
                kind=args.kind,
                layer=args.layer,
                level=args.level,
                name=args.name,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                since=args.since,
                until=args.until,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        ]
    )


def cmd_observability_prune(args: argparse.Namespace) -> None:
    removed = _plane(args).prune_observability(
        older_than=args.older_than,
        keep_last=args.keep_last,
    )
    _print({"removed": removed})


def cmd_memory_summarize_actions(args: argparse.Namespace) -> None:
    _print(
        _plane(args).summarize_actions_to_memory(
            agent_id=args.agent,
            since=args.since,
            created_by=args.created_by,
            write=not args.dry_run,
        )
    )


def cmd_workflow_decisions(args: argparse.Namespace) -> None:
    """wf-02: list every human-decision gate in a workflow or live run."""
    cp = _plane(args)
    target = args.id_or_slug
    if target.startswith("run_"):
        _print(cp.workflow_run_decisions(target))
    else:
        _print(cp.workflow_decisions(target, tenant_id=args.tenant_id))


def cmd_workflow_start(args: argparse.Namespace) -> None:
    """wf-03: start a workflow run, optionally with pre-supplied approval
    decisions so the run can advance through approval gates unattended."""
    cp = _plane(args)
    pre_decisions: Dict[str, str] = {}
    for spec in args.pre_decision or []:
        if "=" not in spec:
            raise MACError(
                "--pre-decision expects <node_key>=approved|rejected (got %r)" % spec
            )
        key, _, value = spec.partition("=")
        pre_decisions[key.strip()] = value.strip().lower()
    input_obj = _json_arg(args.input, {})
    _print(
        cp.start_workflow(
            args.workflow_id_or_slug,
            started_by=args.started_by,
            input=input_obj,
            tenant_id=args.tenant_id,
            pre_decisions=pre_decisions or None,
        )
    )


def cmd_notifier_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_notifier_channel(
            args.name,
            args.channel_type,
            event_types=_csv(args.event_types),
            target=_json_arg(args.target, {}),
            metadata=_json_arg(args.metadata, {}),
            enabled=not args.disabled,
        )
    )


def cmd_notifier_list(args: argparse.Namespace) -> None:
    _print(
        [
            channel.to_dict()
            for channel in _plane(args).list_notifier_channels(
                enabled=args.enabled,
                channel_type=args.channel_type,
            )
        ]
    )


def cmd_notifier_delete(args: argparse.Namespace) -> None:
    _plane(args).delete_notifier_channel(args.channel_id_or_name)
    _print({"deleted": args.channel_id_or_name})


def cmd_notifier_deliver(args: argparse.Namespace) -> None:
    _print(
        _plane(args).deliver_pending_notifications(
            limit=args.limit,
            notification_id=args.notification_id,
        )
    )


def cmd_rollout_list(args: argparse.Namespace) -> None:
    _print([rollout.to_dict() for rollout in _plane(args).list_rollouts(args.tenant_id, args.channel)])


def cmd_rollout_advance(args: argparse.Namespace) -> None:
    _print(_plane(args).advance_rollout(args.rollout_id, args.action, args.actor, _json_arg(args.detail, {})))


def cmd_rollout_rescue(args: argparse.Namespace) -> None:
    rollout, task = _plane(args).rescue_rollout(
        args.rollout_id,
        args.actor,
        args.reason,
        _json_arg(args.detail, {}),
    )
    _print({"rollout": rollout.to_dict(), "task": task.to_dict()})


def cmd_rollout_verify_artifact(args: argparse.Namespace) -> None:
    _print(
        _plane(args).verify_rollout_artifact(
            args.rollout_id,
            args.artifact_uri,
            args.artifact_hash,
            args.actor,
        )
    )


def cmd_rollout_health(args: argparse.Namespace) -> None:
    _print(
        _plane(args).evaluate_rollout_health(
            args.rollout_id,
            _json_arg(args.checks, {}),
            args.actor,
        )
    )


def _set(func: Callable[[argparse.Namespace], None], parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(func=func)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac", description="Multi-agent coordinator control plane")
    # Transport selection (see mac.dispatch.resolve_dispatch for resolution).
    # --db is no longer auto-defaulted: the CLI either targets a hub (default
    # when MAC_API_URL or fleets.yaml is configured) or an explicit SQLite
    # path. Silent fallback to ./mac.db is gone.
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path (local mode). Use this for offline work, "
        "`init`, and `migrate`. When unset and no hub is configured, mac "
        "refuses to run rather than writing to ./mac.db silently.",
    )
    parser.add_argument(
        "--hub-url",
        default=None,
        help="MAC hub URL (hub mode). Falls back to $MAC_API_URL / "
        "$MAC_URL / $MAC_HUB_URL, then ~/.mac/fleets.yaml for --fleet.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for hub mode. Falls back to $MAC_API_TOKEN "
        "(or $MAC_API_TOKEN__<FLEET> when --fleet is set).",
    )
    parser.add_argument(
        "--fleet",
        default=None,
        help="Fleet name; selects MAC_API_TOKEN__<FLEET> and "
        "~/.mac/fleets.yaml entry.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    _set(cmd_init, sub.add_parser("init", help="initialize the SQLite store"))

    # mac config migrate-env-namespace --fleet <name> [--env-file ...]
    config = sub.add_parser("config", help="configuration helpers").add_subparsers(
        dest="config_command", required=True
    )
    migrate_env = config.add_parser(
        "migrate-env-namespace",
        help="add fleet-scoped variants of flat MAC_* credentials in ~/.mac/.env (mac-g55y)",
    )
    migrate_env.add_argument(
        "--fleet",
        required=True,
        help="fleet name to scope credentials under (e.g. rocky, jordanh-hub)",
    )
    migrate_env.add_argument(
        "--env-file",
        default=os.path.expanduser("~/.mac/.env"),
        help="path to the env file to migrate (default ~/.mac/.env)",
    )
    migrate_env.add_argument(
        "--drop-legacy",
        action="store_true",
        help="remove the flat unscoped keys after writing the scoped variants",
    )
    _set(cmd_config_migrate_env_namespace, migrate_env)

    tenant = sub.add_parser("tenant", help="tenant boundary commands").add_subparsers(dest="tenant_command", required=True)
    tenant_register = tenant.add_parser("register")
    tenant_register.add_argument("name")
    tenant_register.add_argument("--metadata")
    tenant_register.add_argument("--tenant-id")
    _set(cmd_tenant_register, tenant_register)
    tenant_list = tenant.add_parser("list")
    _set(cmd_tenant_list, tenant_list)

    user = sub.add_parser("user", help="human user identity commands").add_subparsers(dest="user_command", required=True)
    user_register = user.add_parser("register")
    user_register.add_argument("tenant_id")
    user_register.add_argument("handle")
    user_register.add_argument("--display-name")
    user_register.add_argument("--metadata")
    user_register.add_argument("--user-id")
    _set(cmd_user_register, user_register)

    persona = sub.add_parser("persona", help="Hermes persona and memory-scope commands").add_subparsers(dest="persona_command", required=True)
    persona_register = persona.add_parser("register")
    persona_register.add_argument("tenant_id")
    persona_register.add_argument("name")
    persona_register.add_argument("--soul-ref", required=True)
    persona_register.add_argument("--memory-scope", required=True)
    persona_register.add_argument("--metadata")
    persona_register.add_argument("--persona-id")
    _set(cmd_persona_register, persona_register)

    hermes = sub.add_parser("hermes", help="Hermes instance commands").add_subparsers(dest="hermes_command", required=True)
    hermes_register = hermes.add_parser("register")
    hermes_register.add_argument("tenant_id")
    hermes_register.add_argument("name")
    hermes_register.add_argument("--persona-id")
    hermes_register.add_argument("--home-ref")
    hermes_register.add_argument("--status", default="active")
    hermes_register.add_argument("--metadata")
    hermes_register.add_argument("--instance-id")
    _set(cmd_hermes_register, hermes_register)
    hermes_context = hermes.add_parser("context")
    hermes_context.add_argument("instance_id")
    _set(cmd_hermes_context, hermes_context)
    hermes_work_context = hermes.add_parser("work-context")
    hermes_work_context.add_argument("instance_id")
    hermes_work_context.add_argument("--active-only", action="store_true")
    hermes_work_context.add_argument("--task-limit", type=int, default=100)
    _set(cmd_hermes_work_context, hermes_work_context)
    hermes_runtime_proof = hermes.add_parser("runtime-proof")
    hermes_runtime_proof.add_argument("instance_id")
    hermes_runtime_proof.add_argument("--skip-startup-report", action="store_true")
    _set(cmd_hermes_runtime_proof, hermes_runtime_proof)

    binding = sub.add_parser("binding", help="Hermes platform binding commands").add_subparsers(dest="binding_command", required=True)
    binding_register = binding.add_parser("register")
    binding_register.add_argument("tenant_id")
    binding_register.add_argument("hermes_instance_id")
    binding_register.add_argument("platform")
    binding_register.add_argument("external_id")
    binding_register.add_argument("--display-name")
    binding_register.add_argument("--scopes")
    binding_register.add_argument("--metadata")
    binding_register.add_argument("--binding-id")
    _set(cmd_binding_register, binding_register)

    interaction = sub.add_parser("interaction", help="create durable work from Hermes conversation context").add_subparsers(dest="interaction_command", required=True)
    interaction_task = interaction.add_parser("task")
    interaction_task.add_argument("hermes_instance_id")
    interaction_task.add_argument("title")
    interaction_task.add_argument("--user-id")
    interaction_task.add_argument("--platform-binding-id")
    interaction_task.add_argument("--conversation-ref")
    interaction_task.add_argument("--description", default="")
    interaction_task.add_argument("--project")
    interaction_task.add_argument("--priority", type=int, default=0)
    interaction_task.add_argument("--required-capabilities")
    interaction_task.add_argument("--dependencies")
    interaction_task.add_argument("--metadata")
    interaction_task.add_argument("--max-attempts", type=int, default=3)
    interaction_task.add_argument("--actor", default="hermes")
    _set(cmd_interaction_task, interaction_task)

    task = sub.add_parser("task", help="task ledger commands").add_subparsers(dest="task_command", required=True)
    create = task.add_parser("create")
    create.add_argument("title")
    create.add_argument("--description", default="",
                        help="task description (use --description-file for multi-line / shell-hostile content)")
    create.add_argument("--description-file", dest="description_file",
                        help="read description from file path (or '-' for stdin); avoids shell-quoting hazards")
    create.add_argument(
        "--project",
        help="project to tag the task with; defaults to the working directory's "
        "project (git repo name, else cwd basename). Pass --project '' for none.",
    )
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--required-capabilities")
    create.add_argument("--dependencies")
    create.add_argument("--metadata",
                        help="JSON metadata (use --metadata-file for shell-hostile content)")
    create.add_argument("--metadata-file", dest="metadata_file",
                        help="read JSON metadata from file path (or '-' for stdin)")
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument("--actor", default="human")
    create.add_argument("--no-ticket", dest="no_ticket", action="store_true",
                        help="don't write the .tickets/<id>.md mirror for this task")
    create.add_argument("--no-dispatch", dest="no_dispatch", action="store_true",
                        help="stage the task: the loop-mode fleet won't auto-claim it "
                             "(and it's hidden from `task ready`) until started explicitly")
    create.add_argument("--no-decompose", dest="no_decompose", action="store_true",
                        help="handoff/plan-note guard: the executor will not auto-decompose "
                             "this task into child tasks")
    _set(cmd_task_create, create)

    list_tasks = task.add_parser("list")
    list_tasks.add_argument("--state")
    list_tasks.add_argument("--project", help="filter to this project (default: the cwd's project)")
    list_tasks.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_list, list_tasks)

    show = task.add_parser("show")
    show.add_argument("task_id")
    _set(cmd_task_show, show)

    ready = task.add_parser("ready", help="list open tasks ready to work (all deps completed, unclaimed)")
    ready.add_argument("--limit", type=int, default=0)
    ready.add_argument("--project", help="filter to this project (default: the cwd's project)")
    ready.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_ready, ready)

    claim = task.add_parser("claim", help="atomically claim a task for an agent")
    claim.add_argument("task_id")
    claim.add_argument("agent_id")
    _set(cmd_task_claim, claim)

    close = task.add_parser("close", help="transition a task to completed/cancelled with an optional reason")
    close.add_argument("task_id")
    close.add_argument("--reason", default="")
    close.add_argument("--actor", default="human")
    close.add_argument("--no-ticket", dest="no_ticket", action="store_true",
                       help="don't update the .tickets/<id>.md mirror on close")
    close.add_argument("--cancelled", dest="success", action="store_false",
                       help="close as CANCELLED instead of COMPLETED")
    close.set_defaults(success=True)
    _set(cmd_task_close, close)

    reopen = task.add_parser(
        "reopen",
        help="recovery: return a stuck/terminal task (failed/cancelled/blocked) to OPEN for retry or reconciliation",
    )
    reopen.add_argument("task_id")
    reopen.add_argument("--reason", default="")
    reopen.add_argument("--actor", default="human")
    _set(cmd_task_reopen, reopen)

    force_complete = task.add_parser(
        "force-complete",
        help="operator override: mark a task COMPLETED regardless of state/review (bypasses the review gate; audited)",
    )
    force_complete.add_argument("task_id")
    force_complete.add_argument("--reason", default="")
    force_complete.add_argument("--actor", default="human")
    _set(cmd_task_force_complete, force_complete)

    search = task.add_parser("search", help="keyword search across task title and description")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--project", help="filter to this project (default: the cwd's project)")
    search.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_search, search)

    stats = task.add_parser("stats", help="count tasks by state")
    stats.add_argument("--project", help="filter to this project (default: the cwd's project)")
    stats.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_stats, stats)

    start = task.add_parser("start")
    start.add_argument("task_id")
    start.add_argument("agent_id")
    _set(cmd_task_start, start)

    release = task.add_parser(
        "release", help="clear a --no-dispatch hold so the task can auto-dispatch"
    )
    release.add_argument("task_id")
    release.add_argument("--actor", default="human")
    _set(cmd_task_release, release)

    submit = task.add_parser("submit-review")
    submit.add_argument("task_id")
    submit.add_argument("agent_id")
    _set(cmd_task_submit, submit)

    evidence = task.add_parser("evidence")
    evidence.add_argument("task_id")
    evidence.add_argument("--kind", required=True)
    evidence.add_argument("--uri", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--created-by", required=True)
    evidence.add_argument("--checksum")
    evidence.add_argument("--metadata")
    _set(cmd_task_evidence, evidence)

    detect_beads = task.add_parser(
        "detect-beads",
        help="inspect a repo for .beads/ artifacts (read-only)",
    )
    detect_beads.add_argument("repo_path")
    _set(cmd_task_detect_beads, detect_beads)

    migrate_beads = task.add_parser(
        "migrate-beads",
        help="import .beads/issues.jsonl into MAC tasks and emit .tickets/<id>.md",
    )
    migrate_beads.add_argument("repo_path")
    migrate_beads.add_argument("--project", required=True)
    migrate_beads.add_argument("--actor", default="beads-migrator")
    migrate_beads.add_argument("--dry-run", action="store_true")
    migrate_beads.add_argument("--no-tickets", action="store_true",
                               help="skip writing .tickets/<id>.md files")
    migrate_beads.add_argument("--no-memories", action="store_true",
                               help="skip importing bd memories")
    migrate_beads.add_argument("--tickets-only", action="store_true",
                               help="write .tickets/<id>.md mirror only; skip MAC db writes "
                                    "(useful for repos not registered with a MAC hub)")
    _set(cmd_task_migrate_beads, migrate_beads)

    # Connector-aware (preferred): works for any future ticketing system, not
    # just beads. detect-ticketing reports the needs-conversion signal hermes
    # uses to ask the user; convert-ticketing runs the one-way import.
    detect_ticketing = task.add_parser(
        "detect-ticketing",
        help="detect ticketing sources in a repo (.tickets local mirror / .beads foreign) "
        "and whether a one-way conversion should be offered (read-only)",
    )
    detect_ticketing.add_argument("repo_path")
    _set(cmd_task_detect_ticketing, detect_ticketing)

    convert_ticketing = task.add_parser(
        "convert-ticketing",
        help="one-way convert a detected foreign source (e.g. beads) into MAC "
        "ledger tasks plus optional local compatibility files (run after the user agrees)",
    )
    convert_ticketing.add_argument("repo_path")
    convert_ticketing.add_argument("--project", required=True)
    convert_ticketing.add_argument("--actor", default="hermes")
    convert_ticketing.add_argument("--dry-run", action="store_true")
    _set(cmd_task_convert_ticketing, convert_ticketing)

    project = sub.add_parser("project", help="project summary commands").add_subparsers(dest="project_command", required=True)
    project_create = project.add_parser("create")
    project_create.add_argument("name")
    project_create.add_argument("--description", default="")
    project_create.add_argument("--metadata", default="{}")
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--actor", default="human")
    project_create.add_argument("--project-id")
    project_dispatch = project_create.add_mutually_exclusive_group()
    project_dispatch.add_argument(
        "--paused",
        dest="dispatch_paused",
        action="store_true",
        default=True,
        help="stage the project: its tickets will not auto-dispatch until activated (default)",
    )
    project_dispatch.add_argument(
        "--active",
        dest="dispatch_paused",
        action="store_false",
        help="open the project to autonomous dispatch immediately",
    )
    _set(cmd_project_create, project_create)
    project_onboard = project.add_parser(
        "onboard",
        help="create a contract-authoring task from just a git repo URL: clone "
        "a worktree and task a worker to read the repo's own README/AGENTS/PLAN "
        "(+ manifests), then author .mac/project.yaml",
    )
    project_onboard.add_argument(
        "repository_url",
        metavar="repo-url",
        help="https://, git@, ssh:// or git:// remote (e.g. https://github.com/org/repo.git)",
    )
    project_onboard.add_argument(
        "--project",
        help="project name to file the onboarding task under (default: derived from the repo URL)",
    )
    project_onboard.add_argument(
        "--default-branch",
        help="branch to clone for analysis (default: the remote's default branch)",
    )
    project_onboard.add_argument("--title", help="override the onboarding task title")
    project_onboard.add_argument("--priority", type=int, default=0)
    project_onboard.add_argument(
        "--required-capabilities",
        help="comma-separated capabilities required to claim the onboarding task",
    )
    project_onboard.add_argument("--actor", default="human")
    _set(cmd_project_onboard, project_onboard)
    project_pause = project.add_parser(
        "pause", help="hold a project's tickets from autonomous dispatch"
    )
    project_pause.add_argument("project")
    project_pause.add_argument("--actor", default="human")
    _set(cmd_project_pause, project_pause)
    project_activate = project.add_parser(
        "activate", help="open a project to autonomous dispatch"
    )
    project_activate.add_argument("project")
    project_activate.add_argument("--actor", default="human")
    _set(cmd_project_activate, project_activate)
    project_list = project.add_parser("list")
    _set(cmd_project_list, project_list)
    project_show = project.add_parser("show")
    project_show.add_argument("project")
    _set(cmd_project_show, project_show)

    openshell = sub.add_parser("openshell", help="OpenShell sandbox guardrail commands").add_subparsers(dest="openshell_command", required=True)
    osh_reconcile = openshell.add_parser(
        "reconcile",
        help="reconcile fleet OpenShell required/policy/deployment status after host validation",
    )
    osh_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="mutate hub state; default is a dry-run diff",
    )
    osh_reconcile.add_argument(
        "--validated",
        action="store_true",
        help="assert that host runtime validation has passed; required to apply status=active",
    )
    osh_reconcile.add_argument(
        "--agent",
        action="append",
        help="agent id or name to reconcile; repeatable. Defaults to enabled Linux agents in fleets.yaml",
    )
    osh_reconcile.add_argument(
        "--target-fleet",
        help="fleet in fleets.yaml to read when --agent is omitted; defaults to --fleet or the config default",
    )
    osh_reconcile.add_argument(
        "--fleet-config",
        help="path to fleets.yaml (default: $MAC_FLEETS_CONFIG or ~/.mac/fleets.yaml)",
    )
    osh_reconcile.add_argument(
        "--strict",
        action="store_true",
        help="fail if any fleets.yaml-selected agent is missing from the hub registry",
    )
    osh_reconcile.add_argument("--policy-name", default="mac-docker-engine-moby")
    osh_reconcile.add_argument(
        "--policy-file",
        help="policy YAML to create/reuse/update; default deploy/openshell/mac-hermes-policy.yaml",
    )
    osh_reconcile.add_argument(
        "--status",
        default="active",
        choices=("active", "starting", "inactive", "degraded", "failed", "unknown"),
    )
    osh_reconcile.add_argument("--actor", default="human")
    osh_reconcile.add_argument("--runtime", default="docker-engine-moby")
    osh_reconcile.add_argument("--openshell-version", default="0.0.62")
    osh_reconcile.add_argument("--gateway-driver", default="docker")
    osh_reconcile.add_argument("--image", default="localhost/mac-hermes:net")
    osh_reconcile.add_argument("--sandbox-id")
    osh_reconcile.add_argument("--validation-summary")
    osh_reconcile.add_argument("--detail", help="JSON status detail object")
    osh_reconcile.add_argument("--detail-file", help="read JSON status detail from file path or '-'")
    osh_reconcile.add_argument(
        "--no-report-status",
        action="store_true",
        help="only reconcile required resources and policy assignment",
    )
    _set(cmd_openshell_reconcile, osh_reconcile)

    osh_render = openshell.add_parser(
        "render-policy",
        help="render the OpenShell guardrail policy from the operator template for this fleet",
    )
    osh_render.add_argument("--agent-user", required=True, help="home owner on the agent host (e.g. jkh)")
    osh_render.add_argument("--hub-host", required=True, help="MAC hub host (e.g. 100.125.137.89)")
    osh_render.add_argument("--hub-port", type=int, default=8789)
    osh_render.add_argument("--model-gateway-host", help="LLM gateway host (default: hub host)")
    osh_render.add_argument(
        "--image-runtime",
        help="in-image runtime path (e.g. /opt/mac-venv) when the sandbox runs a "
        "prebuilt --from image instead of a host-uploaded runtime; caches -> /tmp",
    )
    osh_render.add_argument("--qdrant-port", type=int, default=6333)
    osh_render.add_argument("--firecrawl-port", type=int, default=3002)
    osh_render.add_argument(
        "--template",
        default=str(Path(__file__).resolve().parents[2] / "deploy" / "openshell" / "mac-hermes-policy.yaml"),
        help="operator policy template path",
    )
    osh_render.add_argument("--into", help="write the rendered policy here (default: stdout)")
    _set(cmd_openshell_render_policy, osh_render)

    osh_policy = openshell.add_parser("policy", help="MAC-managed OpenShell policies").add_subparsers(
        dest="openshell_policy_command", required=True
    )
    osh_policy_create = osh_policy.add_parser("create")
    osh_policy_create.add_argument("name")
    osh_policy_create.add_argument("--policy-text")
    osh_policy_create.add_argument("--policy-file", help="read policy YAML from file path or '-'")
    osh_policy_create.add_argument("--description", default="")
    osh_policy_create.add_argument("--metadata")
    osh_policy_create.add_argument("--metadata-file")
    osh_policy_create.add_argument("--created-by", default="human")
    osh_policy_create.add_argument("--policy-id")
    _set(cmd_openshell_policy_create, osh_policy_create)

    osh_policy_list = osh_policy.add_parser("list")
    osh_policy_list.add_argument("--include-deleted", action="store_true")
    _set(cmd_openshell_policy_list, osh_policy_list)

    osh_policy_show = osh_policy.add_parser("show")
    osh_policy_show.add_argument("policy")
    _set(cmd_openshell_policy_show, osh_policy_show)

    osh_policy_update = osh_policy.add_parser("update")
    osh_policy_update.add_argument("policy")
    osh_policy_update.add_argument("--name")
    osh_policy_update.add_argument("--description")
    osh_policy_update.add_argument("--policy-text")
    osh_policy_update.add_argument("--policy-file")
    osh_policy_update.add_argument("--metadata")
    osh_policy_update.add_argument("--metadata-file")
    osh_policy_update.add_argument("--updated-by", default="human")
    _set(cmd_openshell_policy_update, osh_policy_update)

    osh_policy_delete = osh_policy.add_parser("delete")
    osh_policy_delete.add_argument("policy")
    osh_policy_delete.add_argument("--actor", default="human")
    _set(cmd_openshell_policy_delete, osh_policy_delete)

    osh_policy_render = osh_policy.add_parser("render")
    osh_policy_render.add_argument("policy")
    osh_policy_render.add_argument("--agent-user")
    osh_policy_render.add_argument("--hub-host")
    osh_policy_render.add_argument("--hub-port", type=int)
    osh_policy_render.add_argument("--model-gateway-host")
    osh_policy_render.add_argument("--shared-services")
    osh_policy_render.add_argument("--shared-services-file")
    osh_policy_render.add_argument("--into")
    _set(cmd_openshell_policy_render, osh_policy_render)

    osh_policy_assign = osh_policy.add_parser("assign")
    osh_policy_assign.add_argument("policy")
    osh_policy_assign.add_argument("target_id")
    osh_policy_assign.add_argument("--target-type", default="agent", choices=("agent", "fleet", "host"))
    osh_policy_assign.add_argument("--created-by", default="human")
    _set(cmd_openshell_policy_assign, osh_policy_assign)

    osh_policy_versions = osh_policy.add_parser("versions")
    osh_policy_versions.add_argument("policy")
    _set(cmd_openshell_policy_versions, osh_policy_versions)

    osh_policy_deploy_status = osh_policy.add_parser("deploy-status")
    osh_policy_deploy_status.add_argument("--agent", required=True)
    _set(cmd_openshell_policy_deploy_status, osh_policy_deploy_status)

    osh_status = openshell.add_parser("status")
    osh_status.add_argument("--agent", required=True)
    _set(cmd_openshell_status, osh_status)

    machine = sub.add_parser("machine", help="machine registry commands").add_subparsers(dest="machine_command", required=True)
    machine_register = machine.add_parser("register")
    machine_register.add_argument("hostname")
    machine_register.add_argument("--labels")
    machine_register.add_argument("--resources")
    machine_register.add_argument("--untrusted", action="store_true")
    machine_register.add_argument("--machine-id")
    _set(cmd_machine_register, machine_register)

    agent = sub.add_parser("agent", help="agent registry commands").add_subparsers(dest="agent_command", required=True)
    agent_register = agent.add_parser("register")
    agent_register.add_argument("machine_id")
    agent_register.add_argument("name")
    agent_register.add_argument("--capabilities")
    agent_register.add_argument("--resources")
    agent_register.add_argument("--agent-id")
    agent_register.add_argument("--hermes-instance-id")
    _set(cmd_agent_register, agent_register)

    agent_list = agent.add_parser("list")
    _set(cmd_agent_list, agent_list)

    agent_hardware = agent.add_parser(
        "hardware", help="fleet hardware inventory from self-reported resources.hardware"
    )
    _set(cmd_agent_hardware, agent_hardware)

    heartbeat = agent.add_parser("heartbeat")
    heartbeat.add_argument("agent_id")
    heartbeat.add_argument("--status")
    heartbeat.add_argument("--health-status")
    heartbeat.add_argument("--resources")
    heartbeat.add_argument(
        "--running-digest",
        help="runtime_environments.digest declaring which build this agent is running",
    )
    _set(cmd_agent_heartbeat, heartbeat)

    agent_delete = agent.add_parser(
        "delete",
        help="hard-delete an agent record (removes mood/nap/events/messages; task history is kept)",
    )
    agent_delete.add_argument("agent_id")
    agent_delete.add_argument("--actor", default="human")
    _set(cmd_agent_delete, agent_delete)

    agent_migrate = agent.add_parser(
        "migrate",
        help="move an agent (soul + memory) to a new host; dry-run unless --execute",
    )
    agent_migrate.add_argument("name")
    agent_migrate.add_argument("--to", dest="to_target", required=True, help="destination user@host")
    agent_migrate.add_argument("--from", dest="from_target", help="source user@host (default: current fleets.yaml target)")
    agent_migrate.add_argument("--to-os", default="linux")
    agent_migrate.add_argument("--fleet", help="fleet name (default: auto-resolve from fleets.yaml)")
    agent_migrate.add_argument("--execute", action="store_true", help="run it (default: print the plan)")
    agent_migrate.add_argument("--keep-source", action="store_true", help="don't decommission the source host")
    agent_migrate.add_argument("--retire-source-agent", help="agent_id to `mac agent delete` after migration")
    _set(cmd_agent_migrate, agent_migrate)

    fleet = sub.add_parser("fleet", help="fleet-wide queries").add_subparsers(
        dest="fleet_command", required=True
    )
    fleet_build = fleet.add_parser(
        "build-distribution",
        help="aggregate live agents by running_digest",
    )
    _set(cmd_fleet_build_distribution, fleet_build)

    fleet_refresh = fleet.add_parser(
        "refresh-source",
        aliases=["refresh"],
        help=(
            "ask fleet agents to pull their self-update repo and restart "
            "themselves if HEAD changes"
        ),
    )
    fleet_refresh.add_argument(
        "--sender-agent-id",
        help="registered admin/control agent id to send the message as; defaults to MAC_AGENT_ID",
    )
    fleet_refresh.add_argument(
        "--agent-id",
        action="append",
        help="target one agent id; repeatable. Default targets every agent.",
    )
    fleet_refresh.add_argument("--repo-path")
    fleet_refresh.add_argument("--remote", default="origin")
    fleet_refresh.add_argument("--branch", default="main")
    fleet_refresh.add_argument("--request-id")
    fleet_refresh.add_argument("--no-restart", action="store_true")
    fleet_refresh.add_argument(
        "--restart-service",
        action="append",
        help=(
            "systemd service to restart on hosts where it is installed after a "
            "successful source update; repeatable"
        ),
    )
    _set(cmd_fleet_refresh_source, fleet_refresh)

    # fleet-02: live group awareness for the team.
    fleet_snap = fleet.add_parser(
        "snapshot",
        help="compact view of the fleet: who's online + what each agent is working on",
    )
    fleet_snap.add_argument("--agent", help="exclude this agent id (the caller) from the snapshot")
    _set(cmd_fleet_snapshot, fleet_snap)

    # Phase 1 fleet snapshot: pull/edit/push the editable agent soul text layer.
    fleet_soul_pull = fleet.add_parser(
        "soul-pull",
        help="pull each agent's editable soul text (SOUL/USER/MEMORY.md) into a local tree",
    )
    fleet_soul_pull.add_argument("--fleet", help="fleet name (default: first in fleets.yaml)")
    fleet_soul_pull.add_argument("--into", required=True, help="destination directory for the snapshot")
    fleet_soul_pull.add_argument(
        "--fleets-config", default=str(Path.home() / ".mac" / "fleets.yaml")
    )
    fleet_soul_pull.add_argument(
        "--memory-checksum", action="store_true",
        help="also sha256 the binary memory blobs (reads them remotely; slower)",
    )
    fleet_soul_pull.add_argument(
        "--with-hub", action="store_true",
        help="also capture hub-stored persona + current mood per agent (needs hub access)",
    )
    _set(cmd_fleet_soul_pull, fleet_soul_pull)

    fleet_soul_push = fleet.add_parser(
        "soul-push",
        help="diff an edited soul snapshot vs live and write changes (backup-before-replace)",
    )
    fleet_soul_push.add_argument("--from", dest="from_dir", required=True, help="snapshot directory")
    fleet_soul_push.add_argument(
        "--dry-run", action="store_true", help="show the plan; write nothing (default off)"
    )
    fleet_soul_push.add_argument(
        "--agent", action="append", help="limit to this agent (repeatable)"
    )
    _set(cmd_fleet_soul_push, fleet_soul_push)

    # Phase 2b: export/vet the fleet's Qdrant vector memory.
    fleet_mem_export = fleet.add_parser(
        "memory-export",
        help="export Qdrant vector memory to greppable JSONL for vetting",
    )
    fleet_mem_export.add_argument("--qdrant-url", required=True, help="e.g. http://100.125.137.89:6333")
    fleet_mem_export.add_argument("--agent", help="filter to this agent_id")
    fleet_mem_export.add_argument("--collections", help="CSV of collections (default: mac_memory_medium,mac_memory_long)")
    fleet_mem_export.add_argument("--search", help="case-insensitive substring filter (e.g. a stale name)")
    fleet_mem_export.add_argument("--into", help="write JSONL here (default: stdout)")
    _set(cmd_fleet_memory_export, fleet_mem_export)

    fleet_mem_prune = fleet.add_parser(
        "memory-prune",
        help="DELETE vetted Qdrant point ids from a collection (destructive)",
    )
    fleet_mem_prune.add_argument("--qdrant-url", required=True)
    fleet_mem_prune.add_argument("--collection", required=True)
    fleet_mem_prune.add_argument("--id", action="append", help="point id to delete (repeatable)")
    fleet_mem_prune.add_argument("--from-jsonl", help="delete the ids in this memory-export JSONL")
    _set(cmd_fleet_memory_prune, fleet_mem_prune)

    fleet_refresh = fleet.add_parser(
        "refresh-context",
        help="refresh the live Fleet section in this agent's runtime-context markdown "
        "(what the nap-tick-style timer calls so each session knows its teammates)",
    )
    fleet_refresh.add_argument("--agent", help="this agent's id (excluded from its own fleet view)")
    fleet_refresh.add_argument(
        "--markdown",
        help="runtime-context markdown path (default: $MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN "
        "or ~/.hermes/mac-runtime-context.md)",
    )
    _set(cmd_fleet_refresh_context, fleet_refresh)

    fleet_validate = fleet.add_parser(
        "validate",
        help="validate a declarative mac.fleet_setup.v1 setup spec",
    )
    fleet_validate.add_argument("--spec", required=True)
    fleet_validate.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
    )
    fleet_validate.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
    )
    _set(cmd_fleet_validate_setup, fleet_validate)

    # journal-01: daily snapshots of an agent's soul + memory state so an
    # evolved personality can be restored if its files are ever lost. Local
    # file ops only — no hub/--db needed.
    journal = sub.add_parser(
        "journal",
        help="snapshot / restore an agent's soul + memory state (guards against soul loss)",
    ).add_subparsers(dest="journal_command", required=True)

    journal_snap = journal.add_parser(
        "snapshot",
        help="snapshot SOUL/USER/MEMORY/memories/mood/config to $HOME/.mac/journal/<date>/ "
        "and run MAC_JOURNAL_BACKUP_HOOK if set",
    )
    journal_snap.add_argument("--dir", help="journal root (default $MAC_JOURNAL_DIR or ~/.mac/journal)")
    journal_snap.add_argument("--home", help="agent HERMES_HOME (default $HERMES_HOME or ~/.hermes)")
    journal_snap.add_argument("--date", help="snapshot date label (default today, UTC)")
    journal_snap.add_argument("--agent", help="agent id label (default $MAC_AGENT_ID)")
    journal_snap.add_argument("--no-hook", action="store_true", help="skip the backup hook")
    _set(cmd_journal_snapshot, journal_snap)

    journal_list = journal.add_parser("list", help="list journaled snapshots")
    journal_list.add_argument("--dir", help="journal root (default ~/.mac/journal)")
    _set(cmd_journal_list, journal_list)

    journal_restore = journal.add_parser(
        "restore",
        help="restore an agent's state from a journal date (snapshots current state first, so it's reversible)",
    )
    journal_restore.add_argument("date", help="journal date to restore (e.g. 2026-06-04)")
    journal_restore.add_argument("--dir", help="journal root (default ~/.mac/journal)")
    journal_restore.add_argument("--home", help="agent HERMES_HOME to restore into")
    journal_restore.add_argument("--dry-run", action="store_true", help="show what would be restored, change nothing")
    _set(cmd_journal_restore, journal_restore)

    fleet_doctor = fleet.add_parser(
        "doctor",
        help="run setup doctor checks for a declarative fleet spec",
    )
    fleet_doctor.add_argument("--spec", required=True)
    fleet_doctor.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
    )
    fleet_doctor.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
    )
    _set(cmd_fleet_doctor_setup, fleet_doctor)

    # auth-token-sync-01: recover/re-sync a client's bearer token from the hub.
    fleet_sync_token = fleet.add_parser(
        "sync-token",
        help="pull the hub's current MAC_API_TOKEN into ~/.mac/.env as "
        "MAC_API_TOKEN__<FLEET> (fixes 403 'unknown bearer token' drift)",
    )
    fleet_sync_token.add_argument(
        "--fleet",
        required=True,
        help="fleet name to sync (resolves the hub's ssh target from fleets.yaml)",
    )
    fleet_sync_token.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
        help="path to fleets.yaml (default ~/.mac/fleets.yaml)",
    )
    fleet_sync_token.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
        help="client env file to update (default ~/.mac/.env)",
    )
    _set(cmd_fleet_sync_token, fleet_sync_token)

    # auth-token-sync-01: graceful rotation via the overlapping MAC_API_TOKENS map.
    fleet_rotate_token = fleet.add_parser(
        "rotate-token",
        help="rotate the hub bearer token with an overlap window (dry-run unless --apply)",
    )
    fleet_rotate_token.add_argument(
        "--fleet",
        required=True,
        help="fleet name to rotate",
    )
    fleet_rotate_token.add_argument(
        "--scope",
        action="append",
        help="scope for the new token (repeatable; default admin)",
    )
    fleet_rotate_token.add_argument(
        "--prune",
        action="store_true",
        help="end the overlap: drop all but the current token (run after every "
        "client has synced to the new token)",
    )
    fleet_rotate_token.add_argument(
        "--apply",
        action="store_true",
        help="actually mutate the hub + this client (default: dry-run plan only)",
    )
    fleet_rotate_token.add_argument(
        "--restart",
        action="store_true",
        help="with --apply, also run the hub restart command over SSH",
    )
    fleet_rotate_token.add_argument(
        "--fleets-config",
        default=str(Path.home() / ".mac" / "fleets.yaml"),
        help="path to fleets.yaml (default ~/.mac/fleets.yaml)",
    )
    fleet_rotate_token.add_argument(
        "--env-file",
        default=str(Path.home() / ".mac" / ".env"),
        help="client env file to update (default ~/.mac/.env)",
    )
    _set(cmd_fleet_rotate_token, fleet_rotate_token)

    mood = sub.add_parser(
        "mood",
        help="agent mood overlays (agents self-report; operators query)",
    ).add_subparsers(dest="mood_command", required=True)
    mood_set = mood.add_parser("set", help="record a mood transition")
    mood_set.add_argument("agent_id")
    mood_set.add_argument(
        "mode",
        choices=(
            "warm",
            "cheerful",
            "sad",
            "curt",
            "cold",
            "irritated",
            "angry",
            "enraged",
        ),
    )
    mood_set.add_argument("--set-by", help="actor (defaults to agent_id)")
    mood_set.add_argument("--reason", help="why the agent picked this mode")
    mood_set.add_argument("--ttl-seconds", type=int)
    mood_set.add_argument("--metadata")
    _set(cmd_mood_set, mood_set)
    mood_show = mood.add_parser("show", help="current mood for an agent")
    mood_show.add_argument("agent_id")
    _set(cmd_mood_show, mood_show)
    mood_clear = mood.add_parser("clear", help="end the active overlay")
    mood_clear.add_argument("agent_id")
    mood_clear.add_argument("--cleared-by")
    mood_clear.add_argument("--reason")
    _set(cmd_mood_clear, mood_clear)
    mood_history = mood.add_parser("history", help="mood transitions for an agent")
    mood_history.add_argument("agent_id")
    mood_history.add_argument("--limit", type=int, default=50)
    _set(cmd_mood_history, mood_history)

    nap = sub.add_parser(
        "nap",
        help="agent nap schedule and lifecycle (daily memory consolidation)",
    ).add_subparsers(dest="nap_command", required=True)
    nap_configure = nap.add_parser(
        "configure",
        help="set or refresh an agent's nap schedule (offset defaults to a deterministic hash of agent.name)",
    )
    nap_configure.add_argument("agent_id")
    nap_configure.add_argument(
        "--offset-minutes",
        type=int,
        help="0-359; omit to derive deterministically from agent name",
    )
    nap_configure.add_argument("--window-minutes", type=int, default=15)
    nap_configure.add_argument("--disabled", action="store_true")
    nap_configure.add_argument("--actor")
    _set(cmd_nap_configure, nap_configure)
    nap_show = nap.add_parser("show")
    nap_show.add_argument("agent_id")
    _set(cmd_nap_show, nap_show)
    nap_next = nap.add_parser("next", help="compute the next nap window")
    nap_next.add_argument("agent_id")
    _set(cmd_nap_next, nap_next)
    nap_begin = nap.add_parser(
        "begin",
        help="start a nap; transitions the agent to DRAINING",
    )
    nap_begin.add_argument("agent_id")
    nap_begin.add_argument("--actor")
    nap_begin.add_argument("--detail")
    _set(cmd_nap_begin, nap_begin)
    nap_complete = nap.add_parser(
        "complete",
        help="mark a nap_run completed and restore the agent",
    )
    nap_complete.add_argument("run_id")
    nap_complete.add_argument(
        "--evidence-id",
        help="evidence row (kind='log') with the summary artifact pointer",
    )
    nap_complete.add_argument("--detail")
    nap_complete.add_argument("--actor")
    _set(cmd_nap_complete, nap_complete)
    nap_fail = nap.add_parser("fail", help="mark a nap_run failed and restore the agent")
    nap_fail.add_argument("run_id")
    nap_fail.add_argument("--reason", required=True)
    nap_fail.add_argument("--actor")
    _set(cmd_nap_fail, nap_fail)
    nap_list = nap.add_parser("list", help="list nap_runs")
    nap_list.add_argument("--agent-id")
    _set(cmd_nap_list, nap_list)

    # mem-08 autonomy: run the whole begin → consolidate → complete arc.
    nap_cycle = nap.add_parser(
        "cycle",
        help="run a full nap cycle (begin + consolidate + complete) for "
        "one agent — what the auto-trigger timer calls",
    )
    nap_cycle.add_argument("agent_id")
    nap_cycle.add_argument("--actor")
    nap_cycle.add_argument("--no-embed", action="store_true")
    nap_cycle.add_argument("--no-dreams", action="store_true")
    nap_cycle.add_argument("--qdrant-url")
    _set(cmd_nap_cycle, nap_cycle)

    nap_due = nap.add_parser(
        "due",
        help="list enabled nap_schedules whose current window has opened "
        "and not yet been completed",
    )
    nap_due.add_argument(
        "--as-of",
        help="ISO timestamp to compute due-ness against (default: now)",
    )
    nap_due.add_argument(
        "--format",
        choices=("json", "agent-ids"),
        default="json",
        help="'json' (full due list) or 'agent-ids' (newline-delimited "
        "agent_ids, for piping to `xargs mac nap cycle`)",
    )
    _set(cmd_nap_due, nap_due)

    # mem-08: build per-(task/project) memory summaries for an agent.
    nap_consolidate = nap.add_parser(
        "consolidate",
        help="walk the agent's recent memory_records, summarize by "
        "task/project, write a nap_summary row per group, and embed "
        "into the medium tier (mem-08)",
    )
    nap_consolidate.add_argument("agent_id")
    nap_consolidate.add_argument(
        "--since",
        help="ISO timestamp lower bound; default = last successful nap's "
        "completed_at, or '<beginning>' if there isn't one",
    )
    nap_consolidate.add_argument("--nap-run-id", help="link the summaries to this run")
    nap_consolidate.add_argument("--created-by")
    nap_consolidate.add_argument(
        "--no-embed",
        action="store_true",
        help="skip the vector-writer handoff (summary-only mode; useful "
        "when Qdrant is offline)",
    )
    nap_consolidate.add_argument(
        "--no-dreams",
        action="store_true",
        help="write nap_summary rows only; skip typed mac.dream.v1 artifacts",
    )
    nap_consolidate.add_argument(
        "--qdrant-url",
        help="override the default Qdrant URL passed to the vector writer",
    )
    _set(cmd_nap_consolidate, nap_consolidate)

    dispatch = sub.add_parser("dispatch", help="dispatcher commands").add_subparsers(dest="dispatch_command", required=True)
    assign = dispatch.add_parser("assign")
    assign.add_argument("--lease-seconds", type=int, default=900)
    _set(cmd_dispatch_once, assign)
    tick = dispatch.add_parser("tick")
    tick.add_argument("--lease-seconds", type=int, default=900)
    tick.add_argument("--limit", type=int, default=100)
    _set(cmd_dispatch_tick, tick)

    message = sub.add_parser("message", help="structured message bus commands").add_subparsers(dest="message_command", required=True)
    send = message.add_parser("send")
    send.add_argument("sender_agent_id")
    send.add_argument("--recipient-agent-id")
    send.add_argument("--task-id")
    send.add_argument("--message-type", required=True)
    send.add_argument("--payload", required=True)
    _set(cmd_message_send, send)
    inbox = message.add_parser("inbox")
    inbox.add_argument("agent_id")
    inbox.add_argument("--limit", type=int, default=50)
    _set(cmd_message_inbox, inbox)

    agentbus = sub.add_parser(
        "agentbus",
        help="typed high-throughput agent-to-agent content streams",
    ).add_subparsers(dest="agentbus_command", required=True)
    bus_open = agentbus.add_parser("open")
    bus_open.add_argument("sender_agent_id")
    bus_open.add_argument("--recipient-agent-id")
    bus_open.add_argument("--task-id")
    bus_open.add_argument("--topic", default="content")
    bus_open.add_argument("--content-type", default="application/json")
    bus_open.add_argument("--headers")
    bus_open.add_argument("--stream-id")
    _set(cmd_agentbus_open, bus_open)

    bus_append = agentbus.add_parser("append")
    bus_append.add_argument("stream_id")
    bus_append.add_argument("sender_agent_id")
    bus_append.add_argument("--payload")
    bus_append.add_argument("--content-type")
    bus_append.add_argument(
        "--payload-encoding",
        choices=("json", "text", "base64"),
        default="json",
    )
    bus_append.add_argument("--final", action="store_true")
    _set(cmd_agentbus_append, bus_append)

    bus_close = agentbus.add_parser("close")
    bus_close.add_argument("stream_id")
    bus_close.add_argument("sender_agent_id")
    bus_close.add_argument("--status", choices=("closed", "aborted"), default="closed")
    _set(cmd_agentbus_close, bus_close)

    bus_list = agentbus.add_parser("list")
    bus_list.add_argument("--agent-id")
    bus_list.add_argument("--status", choices=("open", "closed", "aborted"))
    bus_list.add_argument("--limit", type=int, default=100)
    _set(cmd_agentbus_list, bus_list)

    bus_read = agentbus.add_parser("read")
    bus_read.add_argument("stream_id")
    bus_read.add_argument("agent_id")
    bus_read.add_argument("--after-sequence", type=int, default=0)
    bus_read.add_argument("--limit", type=int, default=100)
    _set(cmd_agentbus_read, bus_read)

    bus_publish = agentbus.add_parser("publish")
    bus_publish.add_argument("sender_agent_id")
    bus_publish.add_argument("--recipient-agent-id")
    bus_publish.add_argument("--task-id")
    bus_publish.add_argument("--topic", default="content")
    bus_publish.add_argument("--content-type", default="application/json")
    bus_publish.add_argument("--headers")
    bus_publish.add_argument("--payload")
    bus_publish.add_argument(
        "--payload-encoding",
        choices=("json", "text", "base64"),
        default="json",
    )
    _set(cmd_agentbus_publish, bus_publish)

    bus_repo_update = agentbus.add_parser("repo-update")
    bus_repo_update.add_argument("sender_agent_id")
    bus_repo_update.add_argument("--recipient-agent-id", action="append")
    bus_repo_update.add_argument("--all-agents", action="store_true")
    bus_repo_update.add_argument("--repo-path")
    bus_repo_update.add_argument("--remote", default="origin")
    bus_repo_update.add_argument("--branch", default="main")
    bus_repo_update.add_argument("--request-id")
    bus_repo_update.add_argument("--no-restart", action="store_true")
    bus_repo_update.add_argument("--restart-service", action="append")
    _set(cmd_agentbus_repo_update, bus_repo_update)

    bus_artifact_publish = agentbus.add_parser("artifact-publish")
    bus_artifact_publish.add_argument("sender_agent_id")
    bus_artifact_publish.add_argument(
        "--operation",
        choices=("create", "upsert", "update", "get", "read", "list", "delete"),
        default="upsert",
    )
    bus_artifact_publish.add_argument("--recipient-agent-id", action="append")
    bus_artifact_publish.add_argument("--all-agents", action="store_true")
    bus_artifact_publish.add_argument("--artifact", help="artifact id for get/delete")
    bus_artifact_publish.add_argument("--digest")
    bus_artifact_publish.add_argument("--kind", default="public-artifact")
    bus_artifact_publish.add_argument("--uri")
    bus_artifact_publish.add_argument("--public-url")
    bus_artifact_publish.add_argument("--path", help="path under MAC_PUBLISH_DIR/public URL")
    bus_artifact_publish.add_argument("--publish-dir")
    bus_artifact_publish.add_argument("--sbom-uri")
    bus_artifact_publish.add_argument("--signers", help="comma-separated signer identities")
    bus_artifact_publish.add_argument("--metadata")
    bus_artifact_publish.add_argument("--task-id")
    bus_artifact_publish.add_argument("--request-id")
    _set(cmd_agentbus_artifact_publish, bus_artifact_publish)

    review = sub.add_parser("review", help="review pipeline commands").add_subparsers(dest="review_command", required=True)
    request = review.add_parser("request")
    request.add_argument("task_id")
    request.add_argument("reviewer_agent_id")
    request.add_argument("--actor", default="dispatcher")
    _set(cmd_review_request, request)
    decision = review.add_parser("decision")
    decision.add_argument("review_id")
    decision.add_argument("status")
    decision.add_argument("reviewer_agent_id")
    decision.add_argument("--reason")
    decision.add_argument("--evidence-id")
    _set(cmd_review_decision, decision)

    publish = sub.add_parser("publish")
    publish.add_argument("task_id")
    publish.add_argument("target")
    publish.add_argument("created_by")
    publish.add_argument("--evidence-id")
    _set(cmd_publish, publish)

    pr = sub.add_parser(
        "pull-request",
        help="open or inspect pull/merge requests on the task's git host",
    ).add_subparsers(dest="pull_request_command", required=True)
    pr_open = pr.add_parser("open", help="open a PR/MR on github or gitea")
    pr_open.add_argument("--repo-url", required=True, help="https URL of the git repository")
    pr_open.add_argument("--head", required=True, help="branch name with the change")
    pr_open.add_argument("--base", default=None, help="target branch (default: repo default branch)")
    pr_open.add_argument("--title", default=None)
    pr_open.add_argument("--body", default="")
    pr_open.add_argument(
        "--task-id",
        default=None,
        help="if set, also record a pull_request_opened integration finding",
    )
    _set(cmd_pull_request_open, pr_open)

    secret = sub.add_parser("secret", help="secret boundary commands").add_subparsers(dest="secret_command", required=True)
    secret_set = secret.add_parser("set")
    secret_set.add_argument("name")
    secret_set.add_argument("value", nargs="?", default=None, help="secret value (avoid; prefer --from-stdin)")
    secret_set.add_argument("--from-stdin", action="store_true", help="read value from stdin")
    secret_set.add_argument("--from-file", help="read value from file path")
    secret_set.add_argument("--scopes", required=True)
    secret_set.add_argument("--created-by", required=True)
    _set(cmd_secret_set, secret_set)
    secret_list = secret.add_parser("list")
    _set(cmd_secret_list, secret_list)
    secret_delete = secret.add_parser("delete", help="hard-delete a secret (scrub its value)")
    secret_delete.add_argument("secret", help="secret id or name")
    secret_delete.add_argument("--actor", default="operator")
    _set(cmd_secret_delete, secret_delete)
    secret_rotate = secret.add_parser("rotate", help="rotate a secret's value in place (audited)")
    secret_rotate.add_argument("name", help="secret id or name")
    secret_rotate.add_argument("value", nargs="?", default=None, help="new value (avoid; prefer --from-stdin / --from-file)")
    secret_rotate.add_argument("--from-stdin", action="store_true", help="read the new value from stdin")
    secret_rotate.add_argument("--from-file", help="read the new value from a file path")
    secret_rotate.add_argument("--actor", default="operator")
    _set(cmd_secret_rotate, secret_rotate)
    secret_access = secret.add_parser("access")
    secret_access.add_argument("secret")
    secret_access.add_argument("agent_id")
    secret_access.add_argument("--purpose", required=True)
    _set(cmd_secret_access, secret_access)
    audits = secret.add_parser("audits")
    audits.add_argument("--secret-id")
    _set(cmd_secret_audits, audits)

    runtime = sub.add_parser("runtime", help="runtime boundary commands").add_subparsers(dest="runtime_command", required=True)
    runtime_create = runtime.add_parser("create")
    runtime_create.add_argument("name")
    runtime_create.add_argument("--manifest", required=True)
    runtime_create.add_argument("--created-by", required=True)
    _set(cmd_runtime_create, runtime_create)
    runtime_list = runtime.add_parser("list")
    _set(cmd_runtime_list, runtime_list)
    runtime_delta = runtime.add_parser("delta", help="runtime environment delta lifecycle").add_subparsers(dest="runtime_delta_command", required=True)
    runtime_delta_propose = runtime_delta.add_parser("propose")
    runtime_delta_propose.add_argument("task_id")
    runtime_delta_propose.add_argument("agent_id")
    runtime_delta_propose.add_argument("--package-manager", required=True, choices=("pip", "uv", "npm", "pnpm"))
    runtime_delta_propose.add_argument("--commands", required=True, help="JSON list of install commands")
    runtime_delta_propose.add_argument("--dependencies", required=True, help="JSON list of added dependencies")
    runtime_delta_propose.add_argument("--reason", required=True)
    runtime_delta_propose.add_argument("--project")
    runtime_delta_propose.add_argument("--base-runtime", help="base runtime id or name")
    runtime_delta_propose.add_argument("--base-runtime-digest")
    runtime_delta_propose.add_argument("--lockfile-path")
    runtime_delta_propose.add_argument("--lockfile-digest")
    runtime_delta_propose.add_argument("--evidence-id")
    _set(cmd_runtime_delta_propose, runtime_delta_propose)
    runtime_delta_list = runtime_delta.add_parser("list")
    runtime_delta_list.add_argument("--status", choices=("proposed", "validated", "rejected", "promoted"))
    runtime_delta_list.add_argument("--task-id")
    runtime_delta_list.add_argument("--project")
    runtime_delta_list.add_argument("--limit", type=int, default=200)
    _set(cmd_runtime_delta_list, runtime_delta_list)
    runtime_delta_show = runtime_delta.add_parser("show")
    runtime_delta_show.add_argument("delta")
    _set(cmd_runtime_delta_show, runtime_delta_show)
    runtime_delta_validate = runtime_delta.add_parser("validate")
    runtime_delta_validate.add_argument("delta")
    runtime_delta_validate.add_argument("--actor", default="operator")
    _set(cmd_runtime_delta_validate, runtime_delta_validate)
    runtime_delta_reject = runtime_delta.add_parser("reject")
    runtime_delta_reject.add_argument("delta")
    runtime_delta_reject.add_argument("--actor", default="operator")
    runtime_delta_reject.add_argument("--reason", required=True)
    _set(cmd_runtime_delta_reject, runtime_delta_reject)
    runtime_delta_promote = runtime_delta.add_parser("promote")
    runtime_delta_promote.add_argument("delta")
    runtime_delta_promote.add_argument("--actor", default="operator")
    runtime_delta_promote.add_argument("--runtime-name")
    _set(cmd_runtime_delta_promote, runtime_delta_promote)

    artifact = sub.add_parser(
        "artifact",
        help="artifact registry: canonical record for deliverables (images, packages, tarballs)",
    ).add_subparsers(dest="artifact_command", required=True)
    artifact_register = artifact.add_parser("register")
    artifact_register.add_argument("kind", help="e.g. image, package, tarball, wheel")
    artifact_register.add_argument("digest", help="canonical hash, e.g. sha256:abc...")
    artifact_register.add_argument("uri")
    artifact_register.add_argument("--created-by", required=True)
    artifact_register.add_argument("--sbom-uri")
    artifact_register.add_argument("--signers", help="comma-separated signer identities")
    artifact_register.add_argument("--metadata")
    _set(cmd_artifact_register, artifact_register)
    artifact_list = artifact.add_parser("list")
    artifact_list.add_argument("--kind")
    _set(cmd_artifact_list, artifact_list)
    artifact_show = artifact.add_parser("show")
    artifact_show.add_argument("artifact", help="artifact id or digest")
    _set(cmd_artifact_show, artifact_show)
    artifact_delete = artifact.add_parser("delete")
    artifact_delete.add_argument("artifact", help="artifact id or digest")
    artifact_delete.add_argument("--actor", default="operator")
    _set(cmd_artifact_delete, artifact_delete)

    env_root = sub.add_parser(
        "env",
        help="environments and deployments (artifact -> environment edges)",
    ).add_subparsers(dest="env_command", required=True)
    env_register = env_root.add_parser("register")
    env_register.add_argument("name")
    env_register.add_argument("--tenant-id")
    env_register.add_argument("--channel", default="fleet")
    env_register.add_argument("--promotes-from", help="upstream environment id")
    env_register.add_argument("--metadata")
    env_register.add_argument("--created-by", default="human")
    _set(cmd_env_register, env_register)
    env_list = env_root.add_parser("list")
    env_list.add_argument("--tenant-id")
    env_list.add_argument("--channel")
    _set(cmd_env_list, env_list)
    env_show = env_root.add_parser("show")
    env_show.add_argument("environment", help="environment id or name")
    _set(cmd_env_show, env_show)
    env_deploy = env_root.add_parser(
        "deploy",
        help="record a new active deployment in an environment, retiring the prior one",
    )
    env_deploy.add_argument("environment", help="environment id or name")
    env_deploy.add_argument("artifact", help="artifact id or digest")
    env_deploy.add_argument("--actor", required=True)
    env_deploy.add_argument("--metadata")
    _set(cmd_env_deploy, env_deploy)
    env_current = env_root.add_parser("current")
    env_current.add_argument("environment")
    _set(cmd_env_current, env_current)
    env_deployments = env_root.add_parser("history")
    env_deployments.add_argument("environment")
    _set(cmd_env_deployments, env_deployments)

    bridge = sub.add_parser("bridge", help="external project bridge commands").add_subparsers(dest="bridge_command", required=True)
    bridge_import = bridge.add_parser("import")
    bridge_import.add_argument("source")
    bridge_import.add_argument("external_id")
    bridge_import.add_argument("title")
    bridge_import.add_argument("--payload", default="{}")
    bridge_import.add_argument("--description")
    bridge_import.add_argument("--project")
    bridge_import.add_argument("--priority", type=int, default=0)
    bridge_import.add_argument("--required-capabilities")
    bridge_import.add_argument("--dependencies")
    bridge_import.add_argument("--metadata", default="{}")
    bridge_import.add_argument("--actor", default="bridge")
    _set(cmd_bridge_import, bridge_import)
    bridge_list = bridge.add_parser("list")
    _set(cmd_bridge_list, bridge_list)
    bridge_repository = bridge.add_parser("repository", help="registered project repository").add_subparsers(dest="bridge_repository_command", required=True)
    bridge_repository_register = bridge_repository.add_parser("register")
    bridge_repository_register.add_argument("name")
    bridge_repository_register.add_argument("path")
    bridge_repository_register.add_argument("--source")
    bridge_repository_register.add_argument("--project")
    bridge_repository_register.add_argument("--required-capabilities")
    bridge_repository_register.add_argument("--poll-interval-seconds", type=int, default=60)
    bridge_repository_register.add_argument("--metadata", default="{}")
    bridge_repository_register.add_argument("--disabled", action="store_true")
    bridge_repository_register.add_argument("--actor", default="beads-bridge")
    _set(cmd_bridge_repository_register, bridge_repository_register)
    bridge_repository_list = bridge_repository.add_parser("repos")
    bridge_repository_list.add_argument("--enabled", action="store_true", default=None)
    _set(cmd_bridge_repository_list, bridge_repository_list)
    integrations = sub.add_parser("integrations", help="integration authority observations and findings").add_subparsers(dest="integrations_command", required=True)
    integrations_findings = integrations.add_parser("findings")
    integrations_findings.add_argument("--source-kind")
    integrations_findings.add_argument("--source-id")
    integrations_findings.add_argument("--finding-type")
    integrations_findings.add_argument("--status")
    integrations_findings.add_argument("--severity")
    integrations_findings.add_argument("--limit", type=int, default=100)
    _set(cmd_integrations_findings, integrations_findings)
    integrations_observations = integrations.add_parser("observations")
    integrations_observations.add_argument("--source-kind")
    integrations_observations.add_argument("--source-id")
    integrations_observations.add_argument("--authority")
    integrations_observations.add_argument("--status")
    integrations_observations.add_argument("--limit", type=int, default=100)
    _set(cmd_integrations_observations, integrations_observations)

    memory = sub.add_parser("memory", help="memory and provenance commands").add_subparsers(dest="memory_command", required=True)
    memory_decay = memory.add_parser(
        "decay",
        help="dream-04: forget stale, low-salience memory records (dry-run unless --apply); "
        "curated knowledge (user/project/feedback/deployment_learning/dream/beads_memory) is preserved",
    )
    memory_decay.add_argument("--ttl-days", type=float, default=90.0)
    memory_decay.add_argument("--limit", type=int, default=500)
    memory_decay.add_argument("--apply", action="store_true", help="actually delete (default: dry-run report)")
    _set(cmd_memory_decay, memory_decay)
    memory_add = memory.add_parser("add")
    memory_add.add_argument("--task-id")
    memory_add.add_argument("--subject-type", required=True)
    memory_add.add_argument("--subject-id")
    memory_add.add_argument("--record-type", required=True)
    memory_add.add_argument("--content", required=True)
    memory_add.add_argument("--evidence-id")
    memory_add.add_argument("--created-by", required=True)
    _set(cmd_memory_add, memory_add)
    memory_search = memory.add_parser("search")
    memory_search.add_argument("--task-id")
    memory_search.add_argument("--subject-type")
    memory_search.add_argument("--subject-id")
    memory_search.add_argument(
        "--record-type",
        help="Exact match on record_type (e.g. nap_summary)",
    )
    memory_search.add_argument(
        "--record-type-prefix",
        help="Prefix match on record_type (e.g. 'dream:' matches dream:reflection, dream:lesson)",
    )
    memory_search.add_argument(
        "--created-by",
        help="Filter by creator (e.g. nap-consolidator, agent_rocky)",
    )
    memory_search.add_argument(
        "--since",
        help="ISO-8601 lower bound on created_at (inclusive)",
    )
    memory_search.add_argument(
        "--until",
        help="ISO-8601 upper bound on created_at (inclusive)",
    )
    memory_search.add_argument(
        "--limit",
        type=int,
        help="Maximum number of records to return",
    )
    memory_search.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="asc",
        help="Sort order: asc (oldest first, default) or desc (newest first)",
    )
    _set(cmd_memory_search, memory_search)

    memory_remember = memory.add_parser(
        "remember",
        help="store an ambient project-scoped fact (bd remember equivalent)",
    )
    memory_remember.add_argument("key")
    memory_remember.add_argument("content")
    memory_remember.add_argument("--project", default="default")
    memory_remember.add_argument("--actor", default="human")
    _set(cmd_memory_remember, memory_remember)

    memory_list = memory.add_parser(
        "list",
        help="list project-scoped memories (bd memories equivalent)",
    )
    memory_list.add_argument("--project", default="default")
    _set(cmd_memory_list, memory_list)

    memory_forget = memory.add_parser(
        "forget",
        help="delete a project-scoped memory by key (bd forget equivalent)",
    )
    memory_forget.add_argument("key")
    memory_forget.add_argument("--project", default="default")
    _set(cmd_memory_forget, memory_forget)

    memory_summarize_actions = memory.add_parser(
        "summarize-actions",
        help="write a bounded memory record from action ledger summaries",
    )
    memory_summarize_actions.add_argument("--agent")
    memory_summarize_actions.add_argument("--since")
    memory_summarize_actions.add_argument("--created-by", default="mac")
    memory_summarize_actions.add_argument(
        "--dry-run",
        action="store_true",
        help="return the summary without writing memory",
    )
    _set(cmd_memory_summarize_actions, memory_summarize_actions)

    # mem-07: embed a memory_record into Qdrant + record the vector_ref.
    memory_embed = memory.add_parser(
        "embed",
        help="embed one memory_record into the vector tier (mem-07)",
    )
    memory_embed.add_argument("memory_id")
    memory_embed.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_embed.add_argument(
        "--qdrant-url",
        help="override the default Qdrant URL (MAC_QDRANT_URL/QDRANT_URL env cascade or 127.0.0.1:6333)",
    )
    _set(cmd_memory_embed, memory_embed)

    memory_backfill = memory.add_parser(
        "backfill",
        help="embed every memory_record not yet in the target Qdrant collection (mem-07)",
    )
    memory_backfill.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_backfill.add_argument(
        "--limit", type=int, help="cap embeddings this pass (None = all)"
    )
    memory_backfill.add_argument("--qdrant-url")
    _set(cmd_memory_backfill, memory_backfill)

    memory_health = memory.add_parser(
        "health",
        help="mem-10: memory-tier health snapshot (counts + alerts for "
        "inert vector tier / stalled consolidator / disk bloat)",
    )
    memory_health.add_argument(
        "--nap-interval-hours", type=float, default=24.0,
        help="2× this value is the stalled-consolidator alert threshold",
    )
    memory_health.add_argument("--qdrant-url")
    _set(cmd_memory_health, memory_health)

    memory_recall = memory.add_parser(
        "recall",
        help="vector-tier recall (mem-09): embed query and return top "
        "ranked memory hits with their summaries",
    )
    memory_recall.add_argument("query")
    memory_recall.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_recall.add_argument("--limit", type=int, default=5)
    memory_recall.add_argument(
        "--min-score", type=float,
        help="drop hits below this cosine score (0.0–1.0)",
    )
    memory_recall.add_argument(
        "--project",
        help="server-side filter: only return hits whose payload project matches",
    )
    memory_recall.add_argument(
        "--tenant-id",
        help="server-side filter: only return hits whose payload tenant matches",
    )
    memory_recall.add_argument(
        "--qdrant-url",
        help="override Qdrant URL when running in local mode (--db). "
        "Hub mode reads the Qdrant env cascade on the hub.",
    )
    _set(cmd_memory_recall, memory_recall)

    memory_recall_dreams = memory.add_parser(
        "recall-dreams",
        help="recall typed dream artifacts with scope/kind/confidence filters",
    )
    memory_recall_dreams.add_argument("query")
    memory_recall_dreams.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_recall_dreams.add_argument("--limit", type=int, default=5)
    memory_recall_dreams.add_argument(
        "--min-score",
        type=float,
        help="drop vector hits below this cosine score (0.0-1.0)",
    )
    memory_recall_dreams.add_argument("--project")
    memory_recall_dreams.add_argument("--agent-id")
    memory_recall_dreams.add_argument(
        "--scope", choices=("agent", "project", "fleet")
    )
    memory_recall_dreams.add_argument(
        "--kind",
        choices=(
            "decision_rule",
            "failure_pattern",
            "knowledge_snippet",
            "tool_pattern",
            "routing_signal",
        ),
    )
    memory_recall_dreams.add_argument(
        "--min-confidence", choices=("low", "medium", "high")
    )
    memory_recall_dreams.add_argument("--tenant-id")
    memory_recall_dreams.add_argument(
        "--qdrant-url",
        help="override Qdrant URL when running in local mode (--db). "
        "Hub mode reads the Qdrant env cascade on the hub.",
    )
    _set(cmd_memory_recall_dreams, memory_recall_dreams)

    rollout = sub.add_parser("rollout", help="rollout and rescue commands").add_subparsers(dest="rollout_command", required=True)
    rollout_create = rollout.add_parser("create")
    rollout_create.add_argument("version")
    rollout_create.add_argument("strategy")
    rollout_create.add_argument("--target-percent", type=int, default=10)
    rollout_create.add_argument("--created-by", required=True)
    rollout_create.add_argument("--tenant-id")
    rollout_create.add_argument("--channel", default="fleet")
    rollout_create.add_argument("--runtime")
    rollout_create.add_argument("--artifact-uri")
    rollout_create.add_argument("--artifact-hash")
    rollout_create.add_argument("--health-policy")
    rollout_create.add_argument("--required-eval-set-id")
    _set(cmd_rollout_create, rollout_create)
    rollout_list = rollout.add_parser("list")
    rollout_list.add_argument("--tenant-id")
    rollout_list.add_argument("--channel")
    _set(cmd_rollout_list, rollout_list)
    rollout_advance = rollout.add_parser("advance")
    rollout_advance.add_argument("rollout_id")
    rollout_advance.add_argument("action")
    rollout_advance.add_argument("--actor", required=True)
    rollout_advance.add_argument("--detail")
    _set(cmd_rollout_advance, rollout_advance)
    rollout_artifact = rollout.add_parser("verify-artifact")
    rollout_artifact.add_argument("rollout_id")
    rollout_artifact.add_argument("--artifact-uri", required=True)
    rollout_artifact.add_argument("--artifact-hash", required=True)
    rollout_artifact.add_argument("--actor", required=True)
    _set(cmd_rollout_verify_artifact, rollout_artifact)
    rollout_health = rollout.add_parser("health")
    rollout_health.add_argument("rollout_id")
    rollout_health.add_argument("--checks", required=True)
    rollout_health.add_argument("--actor", required=True)
    _set(cmd_rollout_health, rollout_health)
    rollout_rescue = rollout.add_parser("rescue")
    rollout_rescue.add_argument("rollout_id")
    rollout_rescue.add_argument("--actor", required=True)
    rollout_rescue.add_argument("--reason", required=True)
    rollout_rescue.add_argument("--detail")
    _set(cmd_rollout_rescue, rollout_rescue)

    events = sub.add_parser("events", help="unified audit stream").add_subparsers(
        dest="events_command", required=True
    )
    events_list = events.add_parser(
        "list",
        help="list events across task/rollout/eval_set/secret audit surfaces",
    )
    events_list.add_argument(
        "--subject-type",
        choices=(
            "task",
            "agent",
            "project",
            "fleet",
            "rollout",
            "eval_set",
            "secret",
            "environment",
            "conversation_thread",
            "vector_ref",
        ),
    )
    events_list.add_argument("--subject-id")
    events_list.add_argument("--actor")
    events_list.add_argument("--event-type", help="exact event_type match")
    events_list.add_argument(
        "--prefix",
        help="event_type prefix (e.g. 'rollout.' for all rollout events)",
    )
    events_list.add_argument("--since", help="ISO timestamp lower bound (inclusive)")
    events_list.add_argument("--until", help="ISO timestamp upper bound (inclusive)")
    events_list.add_argument("--limit", type=int, default=100)
    _set(cmd_events_list, events_list)

    action_events = sub.add_parser(
        "action-events",
        help="canonical MAC action event ledger",
    ).add_subparsers(dest="action_events_command", required=True)
    action_events_list = action_events.add_parser("list")
    action_events_list.add_argument("--agent-id")
    action_events_list.add_argument("--task-id")
    action_events_list.add_argument("--session-id")
    action_events_list.add_argument("--sandbox-id")
    action_events_list.add_argument("--policy-id")
    action_events_list.add_argument("--action-type")
    action_events_list.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_list.add_argument("--since")
    action_events_list.add_argument("--until")
    action_events_list.add_argument("--limit", type=int, default=100)
    _set(cmd_action_events_list, action_events_list)

    action_events_stream = action_events.add_parser("stream")
    action_events_stream.add_argument("--agent-id")
    action_events_stream.add_argument("--task-id")
    action_events_stream.add_argument("--session-id")
    action_events_stream.add_argument("--sandbox-id")
    action_events_stream.add_argument("--policy-id")
    action_events_stream.add_argument("--action-type")
    action_events_stream.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_stream.add_argument("--since")
    action_events_stream.add_argument("--limit", type=int, default=100)
    action_events_stream.add_argument("--timeout", type=float, default=0.0)
    action_events_stream.add_argument("--interval", type=float, default=1.0)
    action_events_stream.add_argument("--follow", action="store_true")
    _set(cmd_action_events_stream, action_events_stream)

    action_events_export = action_events.add_parser("export-otlp")
    action_events_export.add_argument("--agent-id")
    action_events_export.add_argument("--task-id")
    action_events_export.add_argument("--session-id")
    action_events_export.add_argument("--sandbox-id")
    action_events_export.add_argument("--policy-id")
    action_events_export.add_argument("--action-type")
    action_events_export.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_export.add_argument("--since")
    action_events_export.add_argument("--until")
    action_events_export.add_argument("--limit", type=int, default=1000)
    _set(cmd_action_events_export_otlp, action_events_export)

    command_audit = sub.add_parser(
        "command-audit", help="short-retention per-agent command log"
    ).add_subparsers(dest="command_audit_command", required=True)
    command_audit_list = command_audit.add_parser(
        "list", help="list audited command start/completion events"
    )
    command_audit_list.add_argument("--agent-id")
    command_audit_list.add_argument("--task-id")
    command_audit_list.add_argument("--command-id")
    command_audit_list.add_argument(
        "--phase", choices=("started", "completed", "failed", "timeout", "error")
    )
    command_audit_list.add_argument("--since", help="ISO timestamp lower bound")
    command_audit_list.add_argument("--until", help="ISO timestamp upper bound")
    command_audit_list.add_argument("--limit", type=int, default=100)
    _set(cmd_command_audit_list, command_audit_list)

    observability = sub.add_parser(
        "observability", help="structured metric/log observations"
    ).add_subparsers(dest="observability_command", required=True)
    observability_list = observability.add_parser(
        "list", help="list structured observability metrics and logs"
    )
    observability_list.add_argument("--kind", choices=("metric", "log"))
    observability_list.add_argument("--layer")
    observability_list.add_argument("--level")
    observability_list.add_argument("--name")
    observability_list.add_argument("--subject-type")
    observability_list.add_argument("--subject-id")
    observability_list.add_argument("--since", help="ISO timestamp lower bound")
    observability_list.add_argument("--until", help="ISO timestamp upper bound")
    observability_list.add_argument("--after-sequence", type=int)
    observability_list.add_argument("--limit", type=int, default=100)
    _set(cmd_observability_list, observability_list)
    observability_prune = observability.add_parser(
        "prune",
        help="delete observability_events older than --older-than (ISO timestamp) "
        "or keep only --keep-last rows; returns the number removed",
    )
    observability_prune.add_argument(
        "--older-than",
        help="ISO timestamp; rows with created_at < this are deleted",
    )
    observability_prune.add_argument(
        "--keep-last",
        type=int,
        help="keep only the most recent N rows by sequence",
    )
    _set(cmd_observability_prune, observability_prune)

    notifier = sub.add_parser(
        "notifier", help="operator notification channel configuration"
    ).add_subparsers(dest="notifier_command", required=True)
    notifier_configure = notifier.add_parser("configure")
    notifier_configure.add_argument("name")
    notifier_configure.add_argument("channel_type", choices=("hermes", "slack", "telegram"))
    notifier_configure.add_argument("--event-types", default="task.*")
    notifier_configure.add_argument("--target", default="{}")
    notifier_configure.add_argument("--metadata", default="{}")
    notifier_configure.add_argument("--disabled", action="store_true")
    _set(cmd_notifier_configure, notifier_configure)
    notifier_list = notifier.add_parser("list")
    notifier_list.add_argument("--enabled", action=argparse.BooleanOptionalAction)
    notifier_list.add_argument("--channel-type", choices=("hermes", "slack", "telegram"))
    _set(cmd_notifier_list, notifier_list)
    notifier_delete = notifier.add_parser("delete")
    notifier_delete.add_argument("channel_id_or_name")
    _set(cmd_notifier_delete, notifier_delete)
    notifier_deliver = notifier.add_parser("deliver")
    notifier_deliver.add_argument("--limit", type=int, default=50)
    notifier_deliver.add_argument("--notification-id")
    _set(cmd_notifier_deliver, notifier_deliver)

    migrate = sub.add_parser(
        "migrate",
        help="one-time migration from external systems",
    ).add_subparsers(dest="migrate_command", required=True)
    migrate_import = migrate.add_parser(
        "import",
        help="replay a JSONL stream of {record: tenant|user|task|evidence|history} rows",
    )
    migrate_import.add_argument("path", help="path to JSONL file")
    _set(cmd_migrate_import, migrate_import)
    migrate_acc = migrate.add_parser(
        "acc",
        help="dry-run or import an ACC SQLite database once",
    )
    migrate_acc.add_argument("acc_db", help="path to ACC SQLite DB, e.g. ~/.acc/data/acc.db")
    migrate_acc.add_argument("--mode", choices=("dry-run", "import"), default="dry-run")
    migrate_acc.add_argument(
        "--allow-active",
        action="store_true",
        help="import claimed/in-progress ACC tasks as requeued mac tasks",
    )
    migrate_acc.add_argument(
        "--audit-limit",
        type=int,
        default=1000,
        help="latest ACC work_audit_events rows to carry as task provenance; 0 skips audit rows",
    )
    migrate_acc.add_argument(
        "--agent-home",
        help="home directory used for soul snapshot path hints; defaults to current home",
    )
    migrate_acc.add_argument("--report", help="write the migration report JSON to this path")
    _set(cmd_migrate_acc, migrate_acc)

    workflow = sub.add_parser(
        "workflow",
        help="workflow inspection (graph definitions, runs, decision gates)",
    ).add_subparsers(dest="workflow_command", required=True)
    workflow_decisions = workflow.add_parser(
        "decisions",
        help="list every human-decision (approval) gate in a workflow or a "
        "live run. Pass a workflow id/slug to see the definition's gates, "
        "or a run id (prefix run_) for live state.",
    )
    workflow_decisions.add_argument(
        "id_or_slug",
        help="workflow id/slug, or workflow-run id (prefix `run_`)",
    )
    workflow_decisions.add_argument(
        "--tenant-id",
        help="scope a slug lookup to a tenant",
    )
    _set(cmd_workflow_decisions, workflow_decisions)

    workflow_start = workflow.add_parser(
        "start",
        help="start a workflow run, optionally with front-loaded approval "
        "decisions so the run can advance unattended.",
    )
    workflow_start.add_argument("workflow_id_or_slug")
    workflow_start.add_argument("--started-by", default="human")
    workflow_start.add_argument("--tenant-id")
    workflow_start.add_argument(
        "--input",
        default=None,
        help="JSON object passed to the workflow as initial input",
    )
    workflow_start.add_argument(
        "--pre-decision",
        action="append",
        default=[],
        metavar="NODE_KEY=approved|rejected",
        help="pre-supplied decision for an approval node; may repeat "
        "(e.g. --pre-decision pm_review=approved --pre-decision qa=rejected)",
    )
    _set(cmd_workflow_start, workflow_start)

    eval_root = sub.add_parser("eval", help="evaluation sets and runs").add_subparsers(
        dest="eval_command", required=True
    )
    eval_set_grp = eval_root.add_parser("set", help="eval set commands").add_subparsers(
        dest="eval_set_command", required=True
    )
    eval_set_create = eval_set_grp.add_parser("create")
    eval_set_create.add_argument("name")
    eval_set_create.add_argument(
        "--scoring", choices=("higher_is_better", "lower_is_better"), default="higher_is_better"
    )
    eval_set_create.add_argument("--description", default="")
    eval_set_create.add_argument("--baseline-score", type=float, default=None)
    eval_set_create.add_argument("--regression-threshold", type=float, default=0.0)
    eval_set_create.add_argument("--metadata")
    eval_set_create.add_argument("--created-by", default="human")
    _set(cmd_eval_set_create, eval_set_create)
    eval_set_list = eval_set_grp.add_parser("list")
    _set(cmd_eval_set_list, eval_set_list)
    eval_set_show = eval_set_grp.add_parser("show")
    eval_set_show.add_argument("eval_set")
    _set(cmd_eval_set_show, eval_set_show)
    eval_set_baseline = eval_set_grp.add_parser("baseline")
    eval_set_baseline.add_argument("eval_set")
    eval_set_baseline.add_argument("baseline_score", type=float)
    eval_set_baseline.add_argument("--actor", default="human")
    _set(cmd_eval_set_baseline, eval_set_baseline)

    eval_run_grp = eval_root.add_parser("run", help="eval run commands").add_subparsers(
        dest="eval_run_command", required=True
    )
    eval_run_record = eval_run_grp.add_parser("record")
    eval_run_record.add_argument("eval_set")
    eval_run_record.add_argument(
        "target_kind",
        choices=("rollout_version", "runtime_environment", "agent_build"),
    )
    eval_run_record.add_argument("target_id")
    eval_run_record.add_argument("score", type=float)
    eval_run_record.add_argument("--detail")
    eval_run_record.add_argument("--evidence-id")
    eval_run_record.add_argument("--created-by", default="human")
    _set(cmd_eval_run_record, eval_run_record)
    eval_run_list = eval_run_grp.add_parser("list")
    eval_run_list.add_argument("--eval-set", dest="eval_set")
    eval_run_list.add_argument("--target-id")
    _set(cmd_eval_run_list, eval_run_list)

    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args)
    except MACError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("invalid JSON: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
