from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from mac.agentbus_control import (
    REPO_UPDATE_CONTENT_TYPE,
    REPO_UPDATE_TOPIC,
    repo_update_payload,
)
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
    print(json.dumps(value, indent=2, sort_keys=True))


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
    _print(
        cp.create_task(
            args.title,
            description=description,
            project=args.project,
            priority=args.priority,
            required_capabilities=_csv(args.required_capabilities),
            dependencies=_csv(args.dependencies),
            metadata=metadata,
            max_attempts=args.max_attempts,
            actor=args.actor,
        )
    )


def cmd_task_list(args: argparse.Namespace) -> None:
    cp = _plane(args)
    _print([task.to_dict() for task in cp.list_tasks(args.state)])


def cmd_task_show(args: argparse.Namespace) -> None:
    _print(_plane(args).task_detail(args.task_id))


def cmd_task_ready(args: argparse.Namespace) -> None:
    """List tasks ready to work — state=open, no unfinished dependencies,
    unclaimed. Matches the ergonomics of `bd ready`."""
    cp = _plane(args)
    from mac.models import TERMINAL_TASK_STATES, TaskState

    rows = cp.store.query_all(
        """
        SELECT * FROM tasks
        WHERE state = ?
          AND owner_agent_id IS NULL
          AND lease_id IS NULL
        ORDER BY priority DESC, created_at
        """,
        (TaskState.OPEN.value,),
    )
    out = []
    for row in rows:
        task = cp._task_from_row(row)
        deps = task.dependencies or []
        unfinished = []
        for dep_id in deps:
            try:
                dep = cp.get_task(dep_id)
            except Exception:  # noqa: BLE001 - missing dep blocks readiness
                unfinished.append(dep_id)
                continue
            if dep.state not in TERMINAL_TASK_STATES:
                unfinished.append(dep_id)
        if not unfinished:
            out.append(task.to_dict())
        if args.limit and len(out) >= args.limit:
            break
    _print(out)


def cmd_task_claim(args: argparse.Namespace) -> None:
    cp = _plane(args)
    task, lease = cp.claim_task(args.task_id, args.agent_id)
    _print({"task": task.to_dict(), "lease_id": lease.id if lease else None})


def cmd_task_close(args: argparse.Namespace) -> None:
    cp = _plane(args)
    from mac.models import TaskState

    detail = {"reason": args.reason} if args.reason else {}
    target = TaskState.COMPLETED.value if args.success else TaskState.CANCELLED.value
    _print(cp.transition_task(args.task_id, target, args.actor, detail).to_dict())


def cmd_task_search(args: argparse.Namespace) -> None:
    cp = _plane(args)
    like = "%" + args.query + "%"
    rows = cp.store.query_all(
        """
        SELECT * FROM tasks
        WHERE title LIKE ? OR description LIKE ?
        ORDER BY priority DESC, created_at DESC
        LIMIT ?
        """,
        (like, like, int(args.limit)),
    )
    _print([cp._task_from_row(row).to_dict() for row in rows])


def cmd_task_stats(args: argparse.Namespace) -> None:
    cp = _plane(args)
    rows = cp.store.query_all(
        "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state ORDER BY state"
    )
    _print({row["state"]: int(row["n"]) for row in rows})


def cmd_project_list(args: argparse.Namespace) -> None:
    _print(_plane(args).list_projects())


def cmd_project_create(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_project(
            args.name,
            description=args.description or "",
            metadata=_json_arg(args.metadata, {}),
            status=args.status,
            actor=args.actor,
            project_id=args.project_id,
        )
    )


def cmd_project_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_project(args.project))


def cmd_task_start(args: argparse.Namespace) -> None:
    _print(_plane(args).start_task(args.task_id, args.agent_id))


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
            args.sender_agent_id,
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
            args.sender_agent_id,
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
            args.sender_agent_id,
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
            args.sender_agent_id,
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
    cp = _plane(args)
    recipients = list(args.recipient_agent_id or [])
    if args.all_agents:
        recipients.extend(agent.id for agent in cp.list_agents())
    recipients = list(dict.fromkeys(item for item in recipients if item))
    if not recipients:
        raise MACError("repo-update requires --recipient-agent-id or --all-agents")
    payload = repo_update_payload(
        repo_path=args.repo_path,
        remote=args.remote,
        branch=args.branch,
        restart=not args.no_restart,
        request_id=args.request_id,
    )
    _print(
        {
            "schema": "mac.agentbus.repo_update_publish.v1",
            "count": len(recipients),
            "streams": [
                cp.publish_agentbus_content(
                    args.sender_agent_id,
                    recipient_agent_id=recipient_id,
                    content_type=REPO_UPDATE_CONTENT_TYPE,
                    payload=payload,
                    topic=REPO_UPDATE_TOPIC,
                )["stream"]
                for recipient_id in recipients
            ],
        }
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


def cmd_secret_access(args: argparse.Namespace) -> None:
    _print(_plane(args).request_secret(args.secret, args.agent_id, args.purpose))


def cmd_secret_audits(args: argparse.Namespace) -> None:
    _print([audit.to_dict() for audit in _plane(args).list_secret_audits(args.secret_id)])


def cmd_runtime_create(args: argparse.Namespace) -> None:
    _print(_plane(args).create_runtime(args.name, _json_arg(args.manifest, {}), args.created_by))


def cmd_runtime_list(args: argparse.Namespace) -> None:
    _print([runtime.to_dict() for runtime in _plane(args).list_runtimes()])


def cmd_artifact_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_artifact(
            args.kind,
            args.digest,
            args.uri,
            args.created_by,
            sbom_uri=args.sbom_uri,
            signers=_csv(args.signers),
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_artifact_list(args: argparse.Namespace) -> None:
    _print([a.to_dict() for a in _plane(args).list_artifacts(args.kind)])


def cmd_artifact_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_artifact(args.artifact))


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


def cmd_bridge_beads_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_beads_repository(
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


def cmd_bridge_beads_repos(args: argparse.Namespace) -> None:
    _print(
        [
            repo.to_dict()
            for repo in _plane(args).list_beads_repositories(enabled=args.enabled)
        ]
    )


def cmd_bridge_beads_poll(args: argparse.Namespace) -> None:
    _print(
        _plane(args).poll_beads_repositories(
            args.repository,
            force=args.force,
            actor=args.actor,
        )
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
            args.task_id,
            args.subject_type,
            args.subject_id,
            args.record_type,
            args.content,
            args.evidence_id,
            args.created_by,
        )
    )


def cmd_memory_search(args: argparse.Namespace) -> None:
    _print([record.to_dict() for record in _plane(args).search_memory(args.task_id, args.subject_type, args.subject_id)])


def cmd_memory_remember(args: argparse.Namespace) -> None:
    """`bd remember` equivalent — store an ambient project-scoped fact
    keyed by name. Subsequent calls with the same key overwrite."""
    cp = _plane(args)
    project = args.project or "default"
    key = args.key
    # Delete any prior record with the same key under this project so
    # the value is updateable in place.
    cp.store.execute(
        "DELETE FROM memory_records WHERE subject_type = 'project' AND subject_id = ? AND record_type = ?",
        (project, "beads_memory:%s" % key),
    )
    _print(
        cp.add_memory(
            None,
            "project",
            project,
            "beads_memory:%s" % key,
            args.content,
            None,
            args.actor,
        )
    )


def _build_vector_writer(args: argparse.Namespace):
    """Construct a VectorWriterService for CLI commands. The Qdrant
    endpoint defaults to MAC_QDRANT_URL or http://127.0.0.1:6333. Tests
    inject the writer directly; this builder is for operator use.
    """
    import os

    from mac.vector_writer_service import VectorWriterService

    cp = _plane(args)
    qdrant_url = (
        getattr(args, "qdrant_url", None)
        or os.environ.get("MAC_QDRANT_URL")
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


def cmd_memory_recall(args: argparse.Namespace) -> None:
    """mem-07: minimal recall — embed query, find top hits in Qdrant.
    The full mem-09 recall API will subsume this."""
    writer = _build_vector_writer(args)
    _print(writer.recall(args.query, tier=args.tier, limit=args.limit))


def cmd_nap_consolidate(args: argparse.Namespace) -> None:
    """mem-08: walk the agent's recent memory_records, write per-group
    summaries, and embed them into the medium tier."""
    cp = _plane(args)
    writer = None
    if not args.no_embed:
        writer = _build_vector_writer(args)
    _print(
        cp.consolidate_nap(
            args.agent_id,
            since=args.since,
            nap_run_id=args.nap_run_id,
            embed_into_medium=not args.no_embed,
            vector_writer=writer,
            created_by=args.created_by,
        )
    )


def cmd_memory_list(args: argparse.Namespace) -> None:
    cp = _plane(args)
    project = args.project or "default"
    rows = cp.store.query_all(
        """
        SELECT * FROM memory_records
        WHERE subject_type = 'project' AND subject_id = ?
          AND record_type LIKE 'beads_memory:%'
        ORDER BY created_at
        """,
        (project,),
    )
    _print(
        [
            {
                "key": row["record_type"].split(":", 1)[1] if ":" in row["record_type"] else row["record_type"],
                "content": row["content"],
                "created_at": row["created_at"],
                "id": row["id"],
            }
            for row in rows
        ]
    )


def cmd_memory_forget(args: argparse.Namespace) -> None:
    cp = _plane(args)
    project = args.project or "default"
    cursor = cp.store.execute(
        "DELETE FROM memory_records WHERE subject_type = 'project' AND subject_id = ? AND record_type = ?",
        (project, "beads_memory:%s" % args.key),
    )
    _print({"deleted": cursor.rowcount, "key": args.key, "project": project})


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
    create.add_argument("--project")
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--required-capabilities")
    create.add_argument("--dependencies")
    create.add_argument("--metadata",
                        help="JSON metadata (use --metadata-file for shell-hostile content)")
    create.add_argument("--metadata-file", dest="metadata_file",
                        help="read JSON metadata from file path (or '-' for stdin)")
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument("--actor", default="human")
    _set(cmd_task_create, create)

    list_tasks = task.add_parser("list")
    list_tasks.add_argument("--state")
    _set(cmd_task_list, list_tasks)

    show = task.add_parser("show")
    show.add_argument("task_id")
    _set(cmd_task_show, show)

    ready = task.add_parser("ready", help="list open tasks ready to work (no unfinished deps, unclaimed)")
    ready.add_argument("--limit", type=int, default=0)
    _set(cmd_task_ready, ready)

    claim = task.add_parser("claim", help="atomically claim a task for an agent")
    claim.add_argument("task_id")
    claim.add_argument("agent_id")
    _set(cmd_task_claim, claim)

    close = task.add_parser("close", help="transition a task to completed/cancelled with an optional reason")
    close.add_argument("task_id")
    close.add_argument("--reason", default="")
    close.add_argument("--actor", default="human")
    close.add_argument("--cancelled", dest="success", action="store_false",
                       help="close as CANCELLED instead of COMPLETED")
    close.set_defaults(success=True)
    _set(cmd_task_close, close)

    search = task.add_parser("search", help="keyword search across task title and description")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    _set(cmd_task_search, search)

    stats = task.add_parser("stats", help="count tasks by state")
    _set(cmd_task_stats, stats)

    start = task.add_parser("start")
    start.add_argument("task_id")
    start.add_argument("agent_id")
    _set(cmd_task_start, start)

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

    project = sub.add_parser("project", help="project summary commands").add_subparsers(dest="project_command", required=True)
    project_create = project.add_parser("create")
    project_create.add_argument("name")
    project_create.add_argument("--description", default="")
    project_create.add_argument("--metadata", default="{}")
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--actor", default="human")
    project_create.add_argument("--project-id")
    _set(cmd_project_create, project_create)
    project_list = project.add_parser("list")
    _set(cmd_project_list, project_list)
    project_show = project.add_parser("show")
    project_show.add_argument("project")
    _set(cmd_project_show, project_show)

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

    fleet = sub.add_parser("fleet", help="fleet-wide queries").add_subparsers(
        dest="fleet_command", required=True
    )
    fleet_build = fleet.add_parser(
        "build-distribution",
        help="aggregate live agents by running_digest",
    )
    _set(cmd_fleet_build_distribution, fleet_build)

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
    _set(cmd_agentbus_repo_update, bus_repo_update)

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
    bridge_beads = bridge.add_parser("beads", help="registered Beads repository bridge").add_subparsers(dest="bridge_beads_command", required=True)
    bridge_beads_register = bridge_beads.add_parser("register")
    bridge_beads_register.add_argument("name")
    bridge_beads_register.add_argument("path")
    bridge_beads_register.add_argument("--source")
    bridge_beads_register.add_argument("--project")
    bridge_beads_register.add_argument("--required-capabilities")
    bridge_beads_register.add_argument("--poll-interval-seconds", type=int, default=60)
    bridge_beads_register.add_argument("--metadata", default="{}")
    bridge_beads_register.add_argument("--disabled", action="store_true")
    bridge_beads_register.add_argument("--actor", default="beads-bridge")
    _set(cmd_bridge_beads_register, bridge_beads_register)
    bridge_beads_repos = bridge_beads.add_parser("repos")
    bridge_beads_repos.add_argument("--enabled", action="store_true", default=None)
    _set(cmd_bridge_beads_repos, bridge_beads_repos)
    bridge_beads_poll = bridge_beads.add_parser("poll")
    bridge_beads_poll.add_argument("--repository")
    bridge_beads_poll.add_argument("--force", action="store_true")
    bridge_beads_poll.add_argument("--actor", default="beads-bridge")
    _set(cmd_bridge_beads_poll, bridge_beads_poll)

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
        help="override the default Qdrant URL (MAC_QDRANT_URL env or 127.0.0.1:6333)",
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

    memory_recall = memory.add_parser(
        "recall",
        help="minimal MVP recall: embed a query and return top hits (mem-09 will subsume)",
    )
    memory_recall.add_argument("query")
    memory_recall.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_recall.add_argument("--limit", type=int, default=5)
    memory_recall.add_argument("--qdrant-url")
    _set(cmd_memory_recall, memory_recall)

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
