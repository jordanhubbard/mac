from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


RUNTIME_CONTEXT_SCHEMA = "mac.hermes.runtime_context.v1"


def stable_id(prefix: str, value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.lower()).strip("_")
    return "%s_%s" % (prefix, safe or "default")


def connection_url(raw: str) -> str:
    parsed = urllib.parse.urlsplit(raw.strip())
    if not parsed.scheme or not parsed.netloc:
        return raw.strip()
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    netloc = host
    if parsed.port is not None:
        netloc = "%s:%s" % (netloc, parsed.port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def set_env(path: Path, updates: Dict[str, Optional[str]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            if updates[key] is not None:
                output.append("%s=%s" % (key, updates[key]))
            seen.add(key)
        else:
            output.append(line)
    for key in sorted(updates):
        if key not in seen and updates[key] is not None:
            output.append("%s=%s" % (key, updates[key]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _repository_contract(workspace_path: Path) -> Dict[str, Any]:
    contract_path = workspace_path / ".mac" / "project.yaml"
    contract: Dict[str, Any] = {
        "path": str(contract_path),
        "exists": contract_path.exists(),
        "schema": None,
        "project": "",
        "required_commands": [],
        "bootstrap_command": "",
        "test_command": "scripts/run-contract-tests.sh",
        "evidence_required": [],
    }
    if not contract_path.exists() or not contract_path.is_file():
        return contract
    try:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        contract["error"] = str(exc)
        return contract
    if not isinstance(data, dict):
        contract["error"] = "repository contract root is not an object"
        return contract
    toolchain = data.get("toolchain") if isinstance(data.get("toolchain"), dict) else {}
    bootstrap = data.get("bootstrap") if isinstance(data.get("bootstrap"), dict) else {}
    test = data.get("test") if isinstance(data.get("test"), dict) else {}
    evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    contract.update(
        {
            "schema": data.get("schema"),
            "project": data.get("project") or "",
            "required_commands": sorted(
                str(item)
                for item in (toolchain.get("required_commands") or [])
                if str(item).strip()
            ),
            "bootstrap_command": str(bootstrap.get("command") or ""),
            "test_command": str(test.get("command") or contract["test_command"]),
            "evidence_required": sorted(
                str(item)
                for item in (evidence.get("required") or [])
                if str(item).strip()
            ),
        }
    )
    return contract


def _session_capability_contract(
    *,
    workspace_path: Path,
    mac_home: Path,
    mac_url: str,
    hermes_instance_id: str,
    agent_id: str,
    repository_contract: Dict[str, Any],
) -> Dict[str, Any]:
    test_command = str(repository_contract.get("test_command") or "scripts/run-contract-tests.sh")
    capabilities = [
        {
            "name": "mac_api",
            "kind": "api",
            "required": True,
            "endpoint": mac_url,
            "purpose": "Read and mutate MAC fleet, agent, task, project, review, memory, and dashboard state.",
        },
        {
            "name": "mac_cli",
            "kind": "cli",
            "required": True,
            "command": "mac --help",
            "expected_path": str(mac_home / "venv" / "bin" / "mac"),
            "purpose": "Operator-grade MAC API access from a shell session.",
        },
        {
            "name": "mac_hermes_cli",
            "kind": "cli",
            "required": True,
            "command": "mac-hermes work-context %s --active-only" % hermes_instance_id,
            "expected_path": str(mac_home / "venv" / "bin" / "mac-hermes"),
            "purpose": "Hermes-safe task/project lifecycle bridge.",
        },
        {
            "name": "shell_execution",
            "kind": "shell",
            "required": True,
            "command": "sh -c true",
            "cwd": str(workspace_path),
            "purpose": "Run non-interactive shell commands from the MAC workspace like a direct Codex or Claude session.",
        },
        {
            "name": "workspace_file_access",
            "kind": "filesystem",
            "required": True,
            "cwd": str(workspace_path),
            "purpose": "Read and write repository source files in the MAC workspace before committing work.",
        },
        {
            "name": "ticket_mirror",
            "kind": "filesystem",
            "required": False,
            "command": "test -d .tickets",
            "cwd": str(workspace_path),
            "purpose": "Read optional local .tickets/<id>.md migration mirrors when present. The MAC task ledger is the canonical issue source.",
        },
        {
            "name": "mac_task_cli",
            "kind": "cli",
            "required": True,
            "command": "mac task ready --limit 5",
            "expected_path": str(mac_home / "venv" / "bin" / "mac"),
            "purpose": "List/create/claim/close tasks via the MAC hub task ledger (the canonical execution store).",
        },
        {
            "name": "git_source_control",
            "kind": "cli",
            "required": True,
            "command": "git status --short --branch",
            "cwd": str(workspace_path),
            "purpose": "Inspect, commit, rebase, and push repository state.",
        },
        {
            "name": "quality_gate",
            "kind": "command",
            "required": True,
            "command": test_command,
            "cwd": str(workspace_path),
            "purpose": "Run the repository contract test gate before completion.",
        },
        {
            "name": "hermes_oneshot_executor",
            "kind": "executor",
            "required": True,
            "command": "mac-task-executor",
            "expected_path": str(mac_home / "bin" / "mac-task-executor"),
            "purpose": "Run a MAC task through a verified coding-agent CLI inside OpenShell with the same workspace and command audit envelope as deployed agents.",
        },
        {
            "name": "command_audit",
            "kind": "cli",
            "required": True,
            "command": "mac-hermes command-audit list --agent-id %s --limit 5" % agent_id,
            "expected_path": str(mac_home / "venv" / "bin" / "mac-hermes"),
            "purpose": "Record and inspect auditable shell work tied to MAC agents and tasks.",
        },
        {
            "name": "web_search",
            "kind": "service",
            "required": True,
            "command": "mac-hermes web-search \"mac release notes\" --limit 1",
            "expected_path": str(mac_home / "venv" / "bin" / "mac-hermes"),
            "environment": ["FIRECRAWL_API_URL", "FIRECRAWL_GATEWAY_URL", "MAC_WEB_SEARCH_PROVIDER"],
            "purpose": "Web search, extraction, and crawl access through the Hermes bridge to the hub Firecrawl-compatible service.",
        },
    ]
    webdav_method = _webdav_publish_method()
    if webdav_method.get("enabled"):
        capabilities.append(
            {
                "name": "public_artifact_publish",
                "kind": "hub_directory_static_http",
                "required": False,
                "endpoint": webdav_method.get("public_url", ""),
                "environment": ["MAC_PUBLISH_DIR", "MAC_PUBLISH_PUBLIC_URL"],
                "purpose": "Publish task artifacts by writing to the hub publish directory and exposing public GET/HEAD reads without HTTP write ingress.",
            }
        )
    direct_session_workflow = [
        "cd %s" % workspace_path,
        "mac task ready --limit 10",
        "mac task stats",
        "mac-hermes runtime-proof %s" % hermes_instance_id,
        "mac-hermes work-context %s --active-only" % hermes_instance_id,
        "mac-hermes tasks --state open",
        "mac-hermes projects",
        "mac-hermes project-items",
        "mac-hermes agents",
        "mac-hermes add-child-task {task_id} --title <child>",
        "mac-hermes claim-next %s --dry-run" % agent_id,
        "mac-hermes command-audit list --agent-id %s --limit 5" % agent_id,
        "mac-hermes web-search \"project dependency release notes\" --limit 5",
    ]
    if webdav_method.get("enabled"):
        direct_session_workflow.append(str(webdav_method.get("example_upload") or ""))
    direct_session_workflow.extend(
        [
            "mac-hermes agent-identity %s" % agent_id,
            "mac-agent --loop --executor %s" % (mac_home / "bin" / "mac-task-executor"),
            "git status --short --branch",
            test_command,
            "git add <files>",
            "git commit -m \"<message>\"",
            "git pull --rebase",
            "git push",
        ]
    )
    return {
        "schema": "mac.hermes.session_capabilities.v1",
        "parity_target": "direct_codex_or_claude_session_in_mac_repo",
        "workspace": {
            "path": str(workspace_path),
            "project_contract": repository_contract,
            "working_directory_rule": "Run repository commands from this workspace unless a task repository contract says otherwise.",
        },
        "capabilities": capabilities,
        "direct_session_workflow": direct_session_workflow,
        "rules": [
            "Treat MAC fleets, agents, tasks, and projects as first-class operational objects.",
            "Use the MAC hub task ledger as the canonical issue source. Treat .tickets/ as optional ignored migration compatibility state when present.",
            "Use `mac task` / `mac memory` for issue lifecycle; do not run `bd`.",
            "Use `mac` / `mac-hermes` for agent CRUD and operational agent state, not ad hoc database edits.",
            "Record command audit phases for shell work that changes or verifies task state.",
            "Use mac-hermes web-search/web-scrape/web-crawl when current external information is required.",
            "Use mac-task-executor through mac-agent loop mode for production coding-agent task execution.",
            "Own the full code lifecycle for repository work: write the change, "
            "run the contract checks, commit/push the task branch, obtain "
            "cross-review, and publish the approved change to the repository "
            "target without routing through a human merge gate.",
            "Continue filing discovered follow-up work as MAC tasks so the "
            "ledger remains complete.",
        ],
    }


def _webdav_publish_method() -> Dict[str, Any]:
    enabled = (os.environ.get("MAC_PUBLISH_WEBDAV_ENABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    public_url = (
        os.environ.get("MAC_PUBLISH_PUBLIC_URL")
        or os.environ.get("MAC_PUBLISH_WEBDAV_URL")
        or os.environ.get("MAC_WEBDAV_PUBLIC_URL")
        or ""
    ).strip().rstrip("/")
    if not enabled or not public_url:
        return {"enabled": False}
    public_path = os.environ.get("MAC_WEBDAV_PUBLIC_PATH") or "/artifacts/"
    publish_dir = (
        os.environ.get("MAC_PUBLISH_DIR")
        or os.environ.get("MAC_WEBDAV_ROOT")
        or str(Path.home() / ".mac" / "public-artifacts")
    )
    return {
        "enabled": True,
        "name": "hub_webdav_public_artifact",
        "kind": "hub_directory_static_http",
        "public_url": connection_url(public_url),
        "public_path": public_path,
        "publish_dir": publish_dir,
        "read": {
            "public": True,
            "methods": ["GET", "HEAD"],
            "auth_required": False,
        },
        "write": {
            "mode": "hub_shared_directory",
            "directory_env": "MAC_PUBLISH_DIR",
            "directory": publish_dir,
            "http_ingress": False,
            "operations": ["create", "update"],
        },
        "crud": {
            "transport": "mac_agentbus",
            "endpoint": "/agentbus/artifact-publish",
            "cli": "mac agentbus artifact-publish",
            "operations": ["upsert", "get", "list", "delete"],
        },
        "rules": [
            "Public readers may only GET or HEAD static artifact bytes.",
            "Do not use HTTP PUT/MKCOL/DELETE for publishing; the public WebDAV/HTTP address is read-only.",
            "Publish bytes by writing them under MAC_PUBLISH_DIR on the hub, then record or update the artifact through MAC/AgentBus.",
            "Record sha256, size, content type, source task, publish path, and public URL as MAC evidence/publication metadata.",
        ],
        "example_upload": 'install -m 0644 ./artifact "$MAC_PUBLISH_DIR/artifact" && mac agentbus artifact-publish "$MAC_AGENT_ID" --operation upsert --path artifact --digest sha256:<digest> --all-agents',
        "example_read": "%s/artifact" % connection_url(public_url),
    }


def _first_class_object_contract(hermes_instance_id: str, agent_id: str) -> Dict[str, Any]:
    return {
        "schema": "mac.hermes.first_class_objects.v1",
        "vocabulary": {
            "primary_objects": ["fleets", "agents", "tasks", "projects"],
            "task_relationships": [
                "blocked_by: dependencies that must complete first",
                "blocks: tasks that depend on this task",
                "children: subtasks created when a parent is too large",
            ],
            "supporting_objects": [
                "machines",
                "tenants/personas/hermes_instances/platform_bindings",
                "claims/leases",
                "reviews/evidence/publications",
                "workflows/workflow_runs",
                "project_repositories/project_items",
                "command_audit/observability_events",
                "memory_records/agentbus_streams/artifacts",
                "notifier_channels/integration_findings",
                "rollouts/environments/evals/secrets",
            ],
            "rule": "Use MAC APIs/CLIs for operational state; use Hermes memory for personality, private memory, and conversation context.",
        },
        "objects": {
            "fleets": {
                "authority": "mac",
                "source_of_truth": "MAC fleet records and fleet-agent membership rows",
                "identity_fields": ["id", "name", "status", "tenant_id", "agent_ids"],
                "api_paths": [
                    "/fleets",
                    "/fleets/{fleet_id_or_name}",
                ],
                "dashboard_state_keys": [
                    "fleets",
                ],
                "dashboard_urls": [
                    "/ui?view=fleets&selected={fleet_id}",
                    "/ui?view=map&selected={fleet_id}",
                ],
                "runtime_rule": "Use fleets to understand the agent collection and hub topology before changing agent state.",
            },
            "tasks": {
                "authority": "mac",
                "source_of_truth": "mac task records and task history",
                "identity_fields": ["id", "title", "state", "project", "owner_agent_id"],
                "api_paths": [
                    "/persona-instances/%s/work-context" % hermes_instance_id,
                    "/persona-instances/%s/tasks" % hermes_instance_id,
                    "/tasks",
                    "/tasks/{task_id}",
                    "/tasks/{task_id}/children",
                    "/tasks/{task_id}/summary",
                    "/agents/%s/claim-next" % agent_id,
                    "/agents/%s/command-audit" % agent_id,
                    "/command-audit?task_id={task_id}",
                    "/tasks/{task_id}/transition",
                ],
                "mac_cli": [
                    "mac task list",
                    "mac task show {task_id}",
                    "mac task create --title ...",
                ],
                "mac_hermes_cli": [
                    "mac-hermes tasks --state open",
                    "mac-hermes task %s <title> --summary <summary> --project <project>"
                    % hermes_instance_id,
                    "mac-hermes task-detail {task_id}",
                    "mac-hermes claim-next %s --dry-run" % agent_id,
                    "mac-hermes claim {task_id} %s" % agent_id,
                    "mac-hermes start {task_id} %s" % agent_id,
                    "mac-hermes add-child-task {task_id} <title>",
                    "mac-hermes transition {task_id} {target_state} --actor %s" % agent_id,
                    "mac-hermes command-audit list --task-id {task_id}",
                ],
                "dashboard_state_keys": [
                    "tasks",
                    "hermes_work_contexts.{hermes_instance_id}.tasks",
                    "hermes_work_contexts.{hermes_instance_id}.relationships.task_dependencies",
                ],
                "dashboard_urls": [
                    "/ui?view=work&selected={task_id}",
                    "/ui?view=tasks&task_state=open&selected={task_id}",
                    "/ui?view=map&selected={task_id}",
                ],
                "runtime_rule": "Refresh MAC work context before selecting, changing, or reporting on tasks.",
            },
            "projects": {
                "authority": "mac",
                "source_of_truth": "MAC project summaries, ProjectItem rows, and registered Beads repositories",
                "identity_fields": ["project", "task_count", "frontier_tasks", "repository_count"],
                "api_paths": [
                    "/persona-instances/%s/work-context" % hermes_instance_id,
                    "/projects",
                    "/projects/{project}",
                    "/bridge/items",
                    "/bridge/repositories",
                ],
                "mac_cli": [
                    "mac project list",
                    "mac project show <project>",
                    "mac bridge import <source> <external_id> <title> --project <project>",
                    "mac bridge list",
                    "mac bridge repository register <name> <path> --project <project>",
                ],
                "mac_hermes_cli": [
                    "mac-hermes projects",
                    "mac-hermes project-detail <project>",
                    "mac-hermes import-project-item <source> <external_id> <title> --project <project>",
                    "mac-hermes project-items",
                    "mac-hermes project-repositories",
                    "mac-hermes register-project-repository <name> <path> --project <project>",
                ],
                "dashboard_state_keys": [
                    "bridge_items",
                    "project_repositories",
                    "hermes_work_contexts.{hermes_instance_id}.projects",
                ],
                "dashboard_urls": [
                    "/ui?view=projects&project={project}",
                    "/ui?view=work&project={project}",
                    "/ui?view=agents&project={project}",
                    "/ui?view=map&project={project}",
                ],
                "runtime_rule": "Treat project frontier, Beads bridge state, and cross-project dependencies as MAC state.",
            },
            "agents": {
                "authority": "mac",
                "source_of_truth": "MAC agent registry, identity composition, leases, and heartbeats",
                "identity_fields": ["id", "name", "status", "health_status", "hermes_instance_id"],
                "api_paths": [
                    "/agents",
                    "/agents/{agent_id}",
                    "/agents/{agent_id}/disable",
                    "/agents/{agent_id}/identity",
                    "/agents/{agent_id}/claim-next",
                    "/agents/{agent_id}/command-audit",
                    "/agents/{agent_id}/heartbeat",
                ],
                "mac_cli": [
                    "mac agent list",
                    "mac agent register <machine_id> <name>",
                    "mac agent heartbeat {agent_id}",
                ],
                "mac_hermes_cli": [
                    "mac-hermes agents",
                    "mac-hermes agent-detail %s" % agent_id,
                    "mac-hermes agent-identity %s" % agent_id,
                    "mac-hermes claim-next %s --dry-run" % agent_id,
                    "mac-hermes command-audit list --agent-id %s" % agent_id,
                ],
                "dashboard_state_keys": [
                    "agents",
                    "hermes_work_contexts.{hermes_instance_id}.agents",
                    "hermes_work_contexts.{hermes_instance_id}.relationships.agent_assignments",
                ],
                "dashboard_urls": [
                    "/ui?view=agents&selected={agent_id}",
                    "/ui?view=work&selected={agent_id}",
                    "/ui?view=map&selected={agent_id}",
                ],
                "runtime_rule": "Use MAC (`mac` / `mac-hermes`) for agent state and operations; Hermes owns personality and private memory.",
            },
        },
    }


def build_runtime_context(
    *,
    agent_name: str,
    fleet_name: str,
    mac_url: str,
    hermes_home: Path,
    mac_home: Path,
    tenant_id: Optional[str] = None,
    persona_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workspace_path: Optional[Path] = None,
) -> Dict[str, Any]:
    resolved_mac_url = connection_url(mac_url) if mac_url else ""
    resolved_tenant_id = tenant_id or stable_id("tenant", fleet_name)
    resolved_persona_id = persona_id or stable_id("persona", agent_name)
    resolved_instance_id = hermes_instance_id or stable_id("hermes", agent_name)
    resolved_agent_id = agent_id or stable_id("agent", agent_name)
    resolved_workspace = workspace_path or Path(
        os.environ.get("MAC_HERMES_WORKSPACE")
        or os.environ.get("SRC_DIR")
        or (mac_home / "src" / "mac")
    )
    repository_contract = _repository_contract(resolved_workspace)
    session_capabilities = _session_capability_contract(
        workspace_path=resolved_workspace,
        mac_home=mac_home,
        mac_url=resolved_mac_url,
        hermes_instance_id=resolved_instance_id,
        agent_id=resolved_agent_id,
        repository_contract=repository_contract,
    )
    publication = {
        "schema": "mac.hermes.publication_methods.v1",
        "methods": [
            method
            for method in [_webdav_publish_method()]
            if method.get("enabled")
        ],
    }
    return {
        "schema": RUNTIME_CONTEXT_SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fleet": fleet_name,
        "agent": {
            "name": agent_name,
            "agent_id": resolved_agent_id,
            "hermes_instance_id": resolved_instance_id,
        },
        "identity": {
            "tenant_id": resolved_tenant_id,
            "persona_id": resolved_persona_id,
            "hermes_instance_id": resolved_instance_id,
            "agent_id": resolved_agent_id,
            "soul_ref": str(hermes_home / "SOUL.md"),
            "memory_scope": str(hermes_home),
            "home_ref": str(hermes_home),
        },
        "authority": {
            "fleets": "mac",
            "tasks": "mac",
            "projects": "mac",
            "agents": "mac",
            "personality": "hermes",
            "user_memory": "hermes",
            "conversation_state": "hermes",
        },
        "endpoints": {
            "mac_api": resolved_mac_url,
            "work_context_api": (
                "%s/persona-instances/%s/work-context" % (resolved_mac_url, resolved_instance_id)
                if resolved_mac_url
                else ""
            ),
        },
        "environment": {
            "MAC_URL": resolved_mac_url,
            "MAC_HERMES_INSTANCE_ID": resolved_instance_id,
            "MAC_WORKER_HERMES_INSTANCE_ID": resolved_instance_id,
            "MAC_PERSONA_INSTANCE_ID": resolved_instance_id,
            "MAC_AGENT_ID": resolved_agent_id,
            "MAC_WORKER_AGENT_NAME": agent_name,
            "MAC_HOME": str(mac_home),
            "HERMES_HOME": str(hermes_home),
            "MAC_HERMES_WORKSPACE": str(resolved_workspace),
            "MAC_PROJECT_CONTRACT_FILE": str(resolved_workspace / ".mac" / "project.yaml"),
        },
        "operations": {
            "refresh_context": [
                "mac hermes work-context %s" % resolved_instance_id,
                "mac-hermes work-context %s --active-only" % resolved_instance_id,
                "mac-hermes work-brief %s" % resolved_instance_id,
                "mac-hermes tasks --state open",
            ],
            "project_bridge": [
                "mac-hermes create-project <name> --description <description>",
                "mac-hermes projects",
                "mac-hermes project-detail <project>",
                "mac-hermes import-project-item <source> <external_id> <title> --project <project>",
                "mac-hermes project-items",
                "mac-hermes project-repositories",
                "mac-hermes register-project-repository <name> <path> --project <project>",
            ],
            "agent_view": [
                "mac-hermes agents",
                "mac-hermes agent-detail %s" % resolved_agent_id,
                "mac-hermes agent-identity %s" % resolved_agent_id,
                "mac-hermes claim-next %s --dry-run" % resolved_agent_id,
                "mac-hermes command-audit list --agent-id %s --limit 20" % resolved_agent_id,
            ],
            "web_research": [
                "mac-hermes web-search \"current project dependency release notes\" --limit 5",
                "mac-hermes web-scrape https://example.com --format markdown",
                "mac-hermes web-crawl https://example.com --limit 1",
                "mac-hermes web-crawl-status {crawl_id}",
            ],
            "create_task": [
                "mac-hermes task %s <title> --summary <summary> --project <project>"
                % resolved_instance_id,
                "mac-hermes claim-next %s --dry-run" % resolved_agent_id,
            ],
            "task_lifecycle": [
                "mac-hermes task-detail {task_id}",
                "mac-hermes claim {task_id} %s" % resolved_agent_id,
                "mac-hermes start {task_id} %s" % resolved_agent_id,
                "mac-hermes add-child-task {task_id} <child-title> --description <summary>",
                "mac-hermes evidence {task_id} --kind test --uri artifact://... --summary ... --created-by %s"
                % resolved_agent_id,
                "mac-hermes command-audit record %s --phase started --argv-json '[\"git\",\"status\"]' --cwd %s --task-id {task_id}"
                % (resolved_agent_id, resolved_workspace),
                "mac-hermes submit-review {task_id} %s" % resolved_agent_id,
                "mac-hermes request-review {task_id} {reviewer_agent_id}",
                "mac-hermes claim-review {review_id} %s" % resolved_agent_id,
                "mac-hermes review-decision {review_id} approved %s --evidence-id {evidence_id}"
                % resolved_agent_id,
                "mac-hermes publish {task_id} git://main %s --evidence-id {evidence_id}"
                % resolved_agent_id,
                "mac-hermes writeback %s {task_id}" % resolved_instance_id,
            ],
            "publish_artifact": [
                method["example_upload"]
                for method in publication["methods"]
                if method.get("example_upload")
            ],
        },
        "publication": publication,
        "first_class_objects": _first_class_object_contract(
            resolved_instance_id,
            resolved_agent_id,
        ),
        "workspace": session_capabilities["workspace"],
        "session_capabilities": session_capabilities,
        "runtime_rules": [
            "MAC is authoritative for fleet, agent, task, project, dependency, assignment, review, and publication state.",
            "Hermes is authoritative for soul, personality, private memory, and conversation state.",
            "Identity is exclusive to this Hermes instance: answer only as %s; do not impersonate, proxy for, or relay as another agent." % agent_name,
            "Refresh MAC work context before selecting, changing, or reporting on work.",
            "If a claimed task is too large, create child tasks; the parent is blocked until those children complete.",
            "Record MAC command audit entries for shell phases that produce task evidence or change repository state.",
            "Use the mac-hermes web research commands instead of undocumented local web-search state.",
            "Do not copy MAC task state into Hermes memory as a source of truth; write only completed-task summaries back to Hermes memory.",
            "MAC agents own the write, cross-review, and merge/publish loop; "
            "humans direct intent and consume outcomes, but are not a required "
            "code-diff quality gate.",
            "File all new work as MAC tasks before or while acting on it so the "
            "ledger remains complete.",
        ],
    }


def render_runtime_markdown(context: Dict[str, Any]) -> str:
    identity = context["identity"]
    agent = context["agent"]
    operations = context["operations"]
    authority = context["authority"]
    first_class = (
        context.get("first_class_objects")
        if isinstance(context.get("first_class_objects"), dict)
        else {}
    )
    object_map = (
        first_class.get("objects")
        if isinstance(first_class.get("objects"), dict)
        else {}
    )
    session = context.get("session_capabilities") if isinstance(context.get("session_capabilities"), dict) else {}
    publication = context.get("publication") if isinstance(context.get("publication"), dict) else {}
    workspace = session.get("workspace") if isinstance(session.get("workspace"), dict) else {}
    project_contract = (
        workspace.get("project_contract")
        if isinstance(workspace.get("project_contract"), dict)
        else {}
    )
    capabilities = [
        item
        for item in (session.get("capabilities") or [])
        if isinstance(item, dict)
    ]
    lines = [
        "# MAC Task and Project Runtime",
        "",
        "This Hermes runtime is coupled to MAC for operational work.",
        "",
        "## Identity",
        "",
        "- Agent: `%s` (`%s`)" % (agent["name"], agent["agent_id"]),
        "- Hermes instance: `%s`" % identity["hermes_instance_id"],
        "- Tenant: `%s`" % identity["tenant_id"],
        "- Persona: `%s`" % identity["persona_id"],
        "- Identity boundary: answer only as `%s`; never claim to be, proxy for, or relay as another agent. If a channel message clearly addresses a different agent, stay silent and let that agent answer."
        % agent["name"],
        "",
        "## Authority",
        "",
        "- Fleets, agents, tasks, projects: `%s`, `%s`, `%s`, `%s`"
        % (authority["fleets"], authority["agents"], authority["tasks"], authority["projects"]),
        "- Personality, user memory, conversation state: `%s`, `%s`, `%s`"
        % (authority["personality"], authority["user_memory"], authority["conversation_state"]),
        "",
        "## Refresh Work Context",
        "",
    ]
    lines.extend("- `%s`" % command for command in operations["refresh_context"])
    lines.extend(["", "## First-Class Objects", ""])
    for name in ("fleets", "agents", "tasks", "projects"):
        object_contract = object_map.get(name) if isinstance(object_map.get(name), dict) else {}
        lines.append(
            "- `%s`: authority `%s`; source `%s`"
            % (
                name,
                object_contract.get("authority") or "unknown",
                object_contract.get("source_of_truth") or "unconfigured",
            )
        )
    vocabulary = first_class.get("vocabulary") if isinstance(first_class.get("vocabulary"), dict) else {}
    if vocabulary:
        relationships = "; ".join(str(item) for item in vocabulary.get("task_relationships") or [])
        supporting = ", ".join(str(item) for item in vocabulary.get("supporting_objects") or [])
        lines.extend(
            [
                "",
                "## MAC Vocabulary",
                "",
                "- Task edges: %s" % relationships,
                "- Supporting objects: %s" % supporting,
                "- Rule: %s" % (vocabulary.get("rule") or ""),
            ]
        )
    lines.extend(["", "## Project Bridge", ""])
    for command in operations.get("project_bridge", []):
        lines.append("- `%s`" % command)
    lines.extend(["", "## Agent View", ""])
    for command in operations.get("agent_view", []):
        lines.append("- `%s`" % command)
    lines.extend(["", "## Dashboard Views", ""])
    for name in ("fleets", "agents", "tasks", "projects"):
        object_contract = object_map.get(name) if isinstance(object_map.get(name), dict) else {}
        for url in object_contract.get("dashboard_urls") or []:
            lines.append("- `%s`: `%s`" % (name, url))
    lines.extend(["", "## Web Research", ""])
    for command in operations.get("web_research", []):
        lines.append("- `%s`" % command)
    lines.extend(["", "## Task Lifecycle", ""])
    for command in operations["create_task"] + operations["task_lifecycle"]:
        lines.append("- `%s`" % command)
    publish_methods = [
        item
        for item in (publication.get("methods") or [])
        if isinstance(item, dict)
    ]
    if publish_methods:
        lines.extend(["", "## Artifact Publication", ""])
        for method in publish_methods:
            lines.append(
                "- `%s`: public reads at `%s`; publish directory `%s`; CRUD via `%s`"
                % (
                    method.get("name") or "publish",
                    method.get("public_url") or "",
                    method.get("publish_dir") or "",
                    (method.get("crud") or {}).get("cli") or "",
                )
            )
            for rule in method.get("rules") or []:
                lines.append("- %s" % rule)
            if method.get("example_upload"):
                lines.append("- `%s`" % method["example_upload"])
    lines.extend(["", "## Direct Session Parity", ""])
    lines.append("- Workspace: `%s`" % (workspace.get("path") or context["environment"].get("MAC_HERMES_WORKSPACE") or "unconfigured"))
    lines.append("- Repository contract: `%s`" % (project_contract.get("path") or "unconfigured"))
    if project_contract.get("project"):
        lines.append("- Project: `%s`" % project_contract["project"])
    if project_contract.get("test_command"):
        lines.append("- Quality gate: `%s`" % project_contract["test_command"])
    lines.extend(["", "### Required Session Capabilities", ""])
    for capability in capabilities:
        command = capability.get("command") or capability.get("endpoint") or capability.get("name")
        lines.append(
            "- `%s`: %s (`%s`)"
            % (
                capability.get("name"),
                capability.get("purpose") or capability.get("kind") or "available to Hermes",
                command,
            )
        )
    workflow = [str(item) for item in (session.get("direct_session_workflow") or []) if str(item).strip()]
    if workflow:
        lines.extend(["", "### Direct Work Loop", ""])
        lines.extend("- `%s`" % command for command in workflow)
    lines.extend(
        [
            "",
            "## Runtime Rules",
            "",
        ]
    )
    lines.extend("- %s" % rule for rule in context["runtime_rules"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# fleet-02: live fleet snapshot injected into the runtime context, so every
# agent's session always knows what its teammates are doing. A timer refreshes
# the delimited block below (mac fleet refresh-context); the prompt builder
# loads the whole markdown, so the block lands in each agent's system prompt.
# ---------------------------------------------------------------------------

FLEET_SECTION_BEGIN = "<!-- mac:fleet:begin -->"
FLEET_SECTION_END = "<!-- mac:fleet:end -->"


def render_fleet_section(snapshot: Dict[str, Any]) -> str:
    """Render a fleet snapshot (services.ControlPlane.fleet_snapshot) as a
    markdown block for the runtime context."""
    members = snapshot.get("members") or []
    lines = [
        FLEET_SECTION_BEGIN,
        "## Fleet — your teammates (live)",
        "",
        "_Other agents on the fleet and what they're doing right now (refreshed %s). "
        "You keep your own identity; use the `fleet` tool to see more or message them._"
        % snapshot.get("generated_at", "?"),
        "",
    ]
    if not members:
        lines.append("- (no other agents currently online)")
    for m in members:
        doing = m.get("current_task_title")
        if not doing:
            doing = ("task %s" % m.get("current_task_id")) if m.get("current_task_id") else "idle"
        lines.append(
            "- **%s** [%s/%s] — %s" % (m.get("name", "?"), m.get("status", "?"), m.get("health", "?"), doing)
        )
    lines.append(FLEET_SECTION_END)
    return "\n".join(lines)


def refresh_fleet_section(markdown_path: Path, section: str) -> None:
    """Insert/replace the delimited fleet block in the runtime-context markdown.
    Idempotent — running it repeatedly just refreshes the block in place."""
    text = ""
    if markdown_path.exists():
        text = markdown_path.read_text(encoding="utf-8", errors="ignore")
    if FLEET_SECTION_BEGIN in text and FLEET_SECTION_END in text:
        pre = text.split(FLEET_SECTION_BEGIN, 1)[0].rstrip()
        post = text.split(FLEET_SECTION_END, 1)[1].lstrip()
        text = (pre + "\n\n" + section + "\n\n" + post).rstrip() + "\n"
    else:
        text = (text.rstrip() + "\n\n" + section + "\n").lstrip("\n")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(text, encoding="utf-8")


# mood-01: the agent's current mood overlay, rendered into the same runtime
# context the prompt builder injects so a set mood actually colors behaviour.
MOOD_SECTION_BEGIN = "<!-- mac:mood:begin -->"
MOOD_SECTION_END = "<!-- mac:mood:end -->"


def render_mood_section(overlay: Optional[Dict[str, Any]]) -> str:
    """Render the agent's current mood overlay (a MoodOverlay.to_dict() or None)
    as a delimited markdown block. Returns just the empty delimiters when there
    is no active mood (or an unknown mode) — so refresh clears any stale block."""
    from mac.mood_policy import render_mood_overlay

    body = ""
    if overlay:
        body = render_mood_overlay(str(overlay.get("mode") or ""), overlay.get("reason"))
    if not body:
        return "%s\n%s" % (MOOD_SECTION_BEGIN, MOOD_SECTION_END)
    return "\n".join([MOOD_SECTION_BEGIN, "## Mood — how you feel right now", "", body, MOOD_SECTION_END])


def refresh_mood_section(markdown_path: Path, section: str) -> None:
    """Insert/replace the delimited mood block in the runtime-context markdown.
    Idempotent; clearing the mood writes empty delimiters which is a no-op block."""
    text = ""
    if markdown_path.exists():
        text = markdown_path.read_text(encoding="utf-8", errors="ignore")
    if MOOD_SECTION_BEGIN in text and MOOD_SECTION_END in text:
        pre = text.split(MOOD_SECTION_BEGIN, 1)[0].rstrip()
        post = text.split(MOOD_SECTION_END, 1)[1].lstrip()
        text = (pre + "\n\n" + section + "\n\n" + post).rstrip() + "\n"
    else:
        text = (text.rstrip() + "\n\n" + section + "\n").lstrip("\n")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(text, encoding="utf-8")


def write_runtime_context(
    *,
    context_path: Path,
    markdown_path: Path,
    hermes_env_path: Path,
    agent_name: str,
    fleet_name: str,
    mac_url: str,
    hermes_home: Path,
    mac_home: Path,
    tenant_id: Optional[str] = None,
    persona_id: Optional[str] = None,
    hermes_instance_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    workspace_path: Optional[Path] = None,
) -> Dict[str, Any]:
    context = build_runtime_context(
        agent_name=agent_name,
        fleet_name=fleet_name,
        mac_url=mac_url,
        hermes_home=hermes_home,
        mac_home=mac_home,
        tenant_id=tenant_id,
        persona_id=persona_id,
        hermes_instance_id=hermes_instance_id,
        agent_id=agent_id,
        workspace_path=workspace_path,
    )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    context_path.chmod(0o600)
    markdown_path.write_text(render_runtime_markdown(context), encoding="utf-8")
    markdown_path.chmod(0o600)
    env_updates = {
        "MAC_HERMES_RUNTIME_CONTEXT_FILE": str(context_path),
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN": str(markdown_path),
        "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED": "1",
        "MAC_FLEET_TENANT_ID": context["identity"]["tenant_id"],
        "MAC_HERMES_PERSONA_ID": context["identity"]["persona_id"],
        "MAC_HERMES_INSTANCE_ID": context["identity"]["hermes_instance_id"],
        "MAC_WORKER_HERMES_INSTANCE_ID": context["identity"]["hermes_instance_id"],
        "MAC_PERSONA_INSTANCE_ID": context["identity"]["hermes_instance_id"],
        "MAC_AGENT_ID": context["identity"]["agent_id"],
        "MAC_WORKER_AGENT_NAME": context["agent"]["name"],
        "MAC_WORKER_HOSTNAME": context["agent"]["name"],
        "MAC_URL": context["environment"]["MAC_URL"],
        "MAC_HUB_URL": context["environment"]["MAC_URL"],
        "HERMES_HOME": context["environment"]["HERMES_HOME"],
        "MAC_HERMES_WORKSPACE": context["environment"]["MAC_HERMES_WORKSPACE"],
        "MAC_PROJECT_CONTRACT_FILE": context["environment"]["MAC_PROJECT_CONTRACT_FILE"],
    }
    methods = [
        item
        for item in (context.get("publication", {}).get("methods") or [])
        if isinstance(item, dict)
    ]
    webdav = next((item for item in methods if item.get("name") == "hub_webdav_public_artifact"), None)
    if webdav:
        env_updates["MAC_PUBLISH_WEBDAV_ENABLED"] = "1"
        env_updates["MAC_PUBLISH_WEBDAV_URL"] = str(webdav.get("public_url") or "")
        env_updates["MAC_PUBLISH_PUBLIC_URL"] = str(webdav.get("public_url") or "")
        env_updates["MAC_PUBLISH_DIR"] = str(webdav.get("publish_dir") or "")
        env_updates["MAC_PUBLISH_METHOD"] = "hub_directory_http"
        env_updates["MAC_WEBDAV_PUBLIC_URL"] = str(webdav.get("public_url") or "")
        env_updates["MAC_WEBDAV_PUBLIC_PATH"] = str(webdav.get("public_path") or "/artifacts/")
    set_env(
        hermes_env_path,
        env_updates,
    )
    return context


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="write Hermes-visible MAC runtime context")
    parser.add_argument("context_path")
    parser.add_argument("markdown_path")
    parser.add_argument("hermes_env_path")
    parser.add_argument("--agent-name", default=os.environ.get("AGENT") or os.environ.get("MAC_WORKER_AGENT_NAME", "agent"))
    parser.add_argument("--fleet-name", default=os.environ.get("FLEET_NAME", "mac"))
    parser.add_argument("--mac-url", default=os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL", ""))
    parser.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
    parser.add_argument("--mac-home", default=os.environ.get("MAC_HOME", str(Path.home() / ".mac")))
    parser.add_argument("--tenant-id", default=os.environ.get("MAC_FLEET_TENANT_ID"))
    parser.add_argument("--persona-id", default=os.environ.get("MAC_HERMES_PERSONA_ID"))
    parser.add_argument("--hermes-instance-id", default=os.environ.get("MAC_HERMES_INSTANCE_ID"))
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    parser.add_argument(
        "--workspace",
        default=os.environ.get("MAC_HERMES_WORKSPACE") or os.environ.get("SRC_DIR"),
    )
    args = parser.parse_args(argv)
    context = write_runtime_context(
        context_path=Path(args.context_path).expanduser(),
        markdown_path=Path(args.markdown_path).expanduser(),
        hermes_env_path=Path(args.hermes_env_path).expanduser(),
        agent_name=args.agent_name,
        fleet_name=args.fleet_name,
        mac_url=args.mac_url,
        hermes_home=Path(args.hermes_home).expanduser(),
        mac_home=Path(args.mac_home).expanduser(),
        tenant_id=args.tenant_id,
        persona_id=args.persona_id,
        hermes_instance_id=args.hermes_instance_id,
        agent_id=args.agent_id,
        workspace_path=Path(args.workspace).expanduser() if args.workspace else None,
    )
    print(
        "runtime context: agent=%s hermes_instance=%s mac_url=%s"
        % (
            context["agent"]["agent_id"],
            context["identity"]["hermes_instance_id"],
            context["endpoints"]["mac_api"] or "unconfigured",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
